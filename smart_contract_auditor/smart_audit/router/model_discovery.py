"""
model_discovery.py
Dynamic free-model catalog loader. Replaces the static ROLE_MODELS placeholder
in api_router.py. For each (provider, tier), verifies which candidate model
is actually LIVE -- not just listed on the provider's /models endpoint.

Listing presence is not proof a model serves completions: providers keep
deprecated/inactive models in their catalog. A candidate is only trusted
once a real minimal completion call against it succeeds (see _probe()).
Probing is bounded (MAX_PROBE_ATTEMPTS per provider/tier) and goes through
budget_tracker so it can't itself blow the RPM budget, and results are
cached with the existing 24h TTL so probing happens once per cache cycle,
not once per finding.

Resilience: disk cache (.cache/model_catalog.json, 24h TTL) avoids
re-probing on every run. If a provider's /models query itself fails
(network/auth), falls back to stale cache, then to the first hardcoded
candidate unprobed -- this function must never raise; api_router always
needs *a* model id to try.
"""

import json
import os
import time
from pathlib import Path
from . import budget_tracker as bt

CACHE_PATH = Path(".cache") / "model_catalog.json"
CACHE_TTL_SECONDS = 24 * 60 * 60  # refresh once a day

MODELS_ENDPOINTS = {
    "nvidia": "https://integrate.api.nvidia.com/v1/models",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai/models",
    "openrouter": "https://openrouter.ai/api/v1/models",
}

CHAT_ENDPOINTS = {
    "nvidia": "https://integrate.api.nvidia.com/v1/chat/completions",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
    "openrouter": "https://openrouter.ai/api/v1/chat/completions",
}

API_KEY_ENV = {
    "nvidia": "NVIDIA_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}

CANDIDATES = {
    "nvidia": {
        "light": [
            "nvidia/nemotron-3.5-lightning-30b-a3b",
            "openai/gpt-oss-20b",
            "z-ai/glm-5.3-flash",
            "nvidia/llama-3.1-nemotron-nano-8b-v1",
        ],
        "heavy": [
            "nvidia/nemotron-3-super-120b-a12b",
            "z-ai/glm-5.3-flash",
            "openai/gpt-oss-20b",
        ],
    },
    "gemini": {
        # ordered by daily-request headroom first (light tier needs sustained
        # call volume), not just RPM -- gemma-4-26b and the flash-lite family
        # have far more generous RPD than the general flash models.
        # NOTE: Google's live catalog returns ids prefixed "models/..." --
        # candidates must include that prefix or they can never match live_ids.
        "light": [
            "models/gemma-4-26b-a4b-it",
            "models/gemini-3.5-flash-lite",
            "models/gemini-3.1-flash-lite",
            "models/gemini-flash-lite-latest",
            "models/gemini-3.8-flash",
            "models/gemini-flash-latest",
        ],
        # no reasoning/"pro"-tier model available on the free key -- reuses
        # the best flash-lite/flash models as the least-bad heavy option.
        "heavy": [
            "models/gemini-3.5-flash-lite",
            "models/gemini-3.8-flash",
            "models/gemma-4-26b-a4b-it",
        ],
    },
    "openrouter": {
        "light": [
            "meta-llama/llama-3.3-8b-instruct:free",
            "mistralai/mistral-small-3.1-24b-instruct:free",
            "google/gemma-3-27b-it:free",
            "meta-llama/llama-3.1-8b-instruct:free",
        ],
        "heavy": [
            "meta-llama/llama-3.3-70b-instruct:free",
            "deepseek/deepseek-r1:free",
            "nvidia/nemotron-3-ultra-550b-a55b:free",
            "meta-llama/llama-3.1-70b-instruct:free",
        ],
    },
}

# Used ONLY as a last-resort fallback when NONE of the curated CANDIDATES
# above pass probing -- lets us pick a real, currently-live model straight
# from the live catalog by name heuristics.
TIER_HINTS = {
    "light": ["nano", "mini", "small", "lite", "flash", "8b", "9b", "7b", "4b", "3b", "2b", "1b"],
    "heavy": ["ultra", "large", "pro", "super", "70b", "72b", "405b", "550b", "120b", "34b", "32b"],
}

# OpenRouter mixes free (":free" suffix) and PAID models in the same
# /models response. Falling back to "any live model" without filtering this
# would risk silently picking a paid model and spending real money -- so
# for openrouter specifically, the pool is restricted to ":free" ids before
# any heuristic matching happens.
FREE_SUFFIX = ":free"

# Model names containing any of these are disqualified from the "light" tier
# heuristic pick specifically. These indicate extra hidden reasoning/thinking
# tokens generated before every response, which defeats the point of "light"
# being the fast, high-call-volume tier -- Prosecutor/Defender need many
# quick calls, not careful deliberation (that's what "heavy"/Judge is for).
LIGHT_TIER_EXCLUDE = ["reasoning", "thinking", "-r1", "deepseek-r1", "-vl", "-omni"]

# Model names containing any of these are disqualified from heuristic
# fallback picks entirely, for ANY tier -- they're task-specialized models
# (safety classifiers, translation, transcription, doc parsing, image/audio
# generation, embodied/robotics) that will happily return 200 on a probe
# but produce garbage for a Prosecutor/Defender/Judge text-reasoning prompt.
TASK_SPECIALIZED_EXCLUDE = [
    "safety-guard", "content-safety", "guard",
    "translate", "riva",
    "transcribe", "-tts",
    "parse",
    "diffusion", "diffusiongemma",
    "calibration",
    "robotics-er",
    "muse-glimmer",
]

# Probing costs a real API call each time -- this is a direct tradeoff
# against tight-tier daily budgets. NVIDIA (40 RPM, no daily cap) barely
# notices probing several candidates; Gemini's tight tier (5 RPM/20 RPD)
# can lose a meaningful chunk of that day's budget to probing alone before
# a single real audit call happens. Cached 24h, so it's a once-a-day cost,
# but keep both constants modest for that reason rather than maximizing them.
MAX_PROBE_ATTEMPTS = 8    # total probe calls allowed per (provider, tier)
MAX_LIVE_CANDIDATES = 5   # stop early once this many validated live models are found


def _heuristic_candidates_from_live(provider: str, tier: str, live_ids: set) -> list[str]:
    """Returns a priority-ordered list of live, task-appropriate candidates
    to probe, used only when every curated CANDIDATES entry has failed."""
    pool = live_ids
    if provider == "openrouter":
        pool = {m for m in live_ids if m.endswith(FREE_SUFFIX)}

    pool = {m for m in pool if not any(bad in m.lower() for bad in TASK_SPECIALIZED_EXCLUDE)}

    if tier == "light":
        filtered = {m for m in pool if not any(bad in m.lower() for bad in LIGHT_TIER_EXCLUDE)}
        if filtered:  # only apply if it doesn't wipe out the whole pool
            pool = filtered

    ordered: list[str] = []
    seen: set = set()
    for hint in TIER_HINTS[tier]:
        for m in sorted(m for m in pool if hint in m.lower()):
            if m not in seen:
                ordered.append(m)
                seen.add(m)
    # anything left with no keyword hit at all, appended last
    for m in sorted(pool):
        if m not in seen:
            ordered.append(m)
            seen.add(m)
    return ordered


def _fetch_model_ids(provider: str) -> set | None:
    """Returns set of listed model ids from provider's /models endpoint, or None on failure."""
    import requests

    key = os.environ.get(API_KEY_ENV[provider])
    if not key:
        return None
    try:
        resp = requests.get(
            MODELS_ENDPOINTS[provider],
            headers={"Authorization": f"Bearer {key}"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        # OpenAI-compatible shape: {"data": [{"id": "..."}, ...]}
        return {m["id"] for m in data.get("data", [])}
    except Exception:
        return None


def _probe(provider: str, model: str) -> str:
    """
    Fires one minimal real completion call to verify `model` actually serves
    a response, not just appears in the listing. Returns:
      "live"         -- responded successfully, safe to use
      "dead"         -- confirmed not-found/bad-request for this model id
      "inconclusive" -- rate-limited, network hiccup, or no budget to spare;
                        NOT proof the model is dead, don't cache a negative
                        result for this, just move on to the next candidate.
    Goes through budget_tracker like any real dispatch, so probing can't
    itself blow the RPM budget before the actual audit run starts.
    """
    key = os.environ.get(API_KEY_ENV[provider])
    if not key:
        return "inconclusive"
    if not bt.check_and_increment(provider, model, estimated_tokens=10):
        return "inconclusive"  # no budget to spare on a probe right now

    import requests

    try:
        resp = requests.post(
            CHAT_ENDPOINTS[provider],
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 4},
            timeout=15,
        )
    except Exception:
        return "inconclusive"

    if resp.status_code == 200:
        return "live"
    if resp.status_code == 429:
        return "inconclusive"
    if resp.status_code in (400, 404):
        return "dead"
    return "inconclusive"  # auth/5xx/etc -- not confident enough to blacklist the model itself


def _pick_multi(provider: str, tier: str, live_ids: set | None) -> list[str]:
    """
    Probes curated CANDIDATES in order (skipping any not present in the
    listing, when a listing was available), collecting every one that
    passes probing -- not just the first -- up to MAX_LIVE_CANDIDATES.
    This gives api_router.route() several validated fallbacks per
    provider/tier, so one bad model doesn't force falling through to an
    entirely different provider on every call.

    Falls back to probing the live catalog via heuristics only if NO
    curated candidate passed. Bounded to MAX_PROBE_ATTEMPTS total probe
    calls per (provider, tier) so a mostly-dead provider fails fast.
    Returns validated candidates in priority order; empty list if none
    could be confirmed live.
    """
    candidates = CANDIDATES[provider][tier]

    if live_ids is None:
        # listing itself failed -- no live signal to probe against, best
        # guess is the top hardcoded candidate, unverified.
        return candidates[:1] if candidates else []

    live_found: list[str] = []
    attempts = 0
    for candidate in candidates:
        if candidate not in live_ids:
            continue  # not even listed, don't waste a probe call on it
        if attempts >= MAX_PROBE_ATTEMPTS or len(live_found) >= MAX_LIVE_CANDIDATES:
            break
        attempts += 1
        if _probe(provider, candidate) == "live":
            live_found.append(candidate)

    if live_found:
        return live_found

    if attempts >= MAX_PROBE_ATTEMPTS:
        print(f"[model_discovery] {provider}/{tier}: hit probe attempt cap, no curated candidate confirmed live")
        return []

    # none of our curated candidates are live -- fall back to live,
    # task-appropriate catalog matches instead of giving up on the provider.
    fallback_pool = _heuristic_candidates_from_live(provider, tier, live_ids)
    for candidate in fallback_pool:
        if attempts >= MAX_PROBE_ATTEMPTS or len(live_found) >= MAX_LIVE_CANDIDATES:
            break
        attempts += 1
        if _probe(provider, candidate) == "live":
            live_found.append(candidate)

    if live_found:
        print(
            f"[model_discovery] NOTE: none of the curated {provider}/{tier} "
            f"candidates {candidates} are live -- CANDIDATES list is stale "
            f"and should be updated. Using probed live matches {live_found} for now."
        )
    else:
        print(
            f"[model_discovery] WARNING: no usable model found for {provider}/{tier} "
            f"after {attempts} probe attempts -- curated candidates are stale AND "
            f"no live/task-appropriate match probed successfully. Skipping this provider/tier."
        )
    return live_found


def _load_cache(ignore_ttl: bool = False) -> dict | None:
    if not CACHE_PATH.exists():
        return None
    try:
        data = json.loads(CACHE_PATH.read_text())
    except Exception:
        return None
    if not ignore_ttl and time.time() - data.get("_fetched_at", 0) > CACHE_TTL_SECONDS:
        return None
    return data


def _save_cache(catalog: dict) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    out = dict(catalog)
    out["_fetched_at"] = time.time()
    CACHE_PATH.write_text(json.dumps(out, indent=2))


def discover_models(force_refresh: bool = False) -> dict:
    """
    Returns {provider: {light: [model_id, ...], heavy: [model_id, ...]}} --
    each tier maps to a priority-ordered list of validated-live models
    (up to MAX_LIVE_CANDIDATES), not a single pick. api_router.route() can
    fall through to the next candidate on the same provider before hopping
    to a different provider tier.

    Order of preference: fresh cache (<24h, already-probed picks) -> live
    /models query + probe per provider -> stale cache for that provider ->
    top hardcoded candidate as a single-item list, unprobed (only if the
    live query itself failed). Never raises.
    """
    if not force_refresh:
        cached = _load_cache()
        if cached:
            return {k: v for k, v in cached.items() if k != "_fetched_at"}

    stale = _load_cache(ignore_ttl=True)
    catalog: dict = {}

    for provider in CANDIDATES:
        live_ids = _fetch_model_ids(provider)
        if live_ids is None and stale and provider in stale:
            catalog[provider] = stale[provider]  # keep last-known-good
            continue
        catalog[provider] = {
            tier: _pick_multi(provider, tier, live_ids) for tier in CANDIDATES[provider]
        }

    _save_cache(catalog)
    return catalog