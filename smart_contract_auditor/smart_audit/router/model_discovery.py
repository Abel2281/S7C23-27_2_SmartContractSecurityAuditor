"""
model_discovery.py
Dynamic free-model catalog loader. Replaces the static ROLE_MODELS placeholder
in api_router.py. Queries each provider's /models endpoint, picks the first
LIVE match from a priority-ordered candidate list per (provider, tier).

Free-tier catalogs churn (models get deprecated/renamed often). Rather than
inventing ids from nothing, this keeps a hand-curated priority list per
provider/tier and just verifies which candidate is currently actually being
served, falling back down the list if the top choice disappeared.

Resilience: disk cache (output/model_catalog.json, 24h TTL) avoids hitting
every provider's /models endpoint on every run. If a provider is unreachable,
falls back to stale cache, then to the first hardcoded candidate -- this
function must never raise; api_router always needs *a* model id to try.

FIX (post-Phase-3 real-world run): _pick() previously fell back to
candidates[0] even when the live /models query SUCCEEDED and proved every
candidate was dead -- guaranteeing a 404 on dispatch. See _pick() below.
"""

import json
import os
import time
from pathlib import Path

CACHE_PATH = Path(".cache") / "model_catalog.json"
CACHE_TTL_SECONDS = 24 * 60 * 60  # refresh once a day

MODELS_ENDPOINTS = {
    "nvidia": "https://integrate.api.nvidia.com/v1/models",
    "mistral": "https://api.mistral.ai/v1/models",
    "openrouter": "https://openrouter.ai/api/v1/models",
}

API_KEY_ENV = {
    "nvidia": "NVIDIA_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}

# priority-ordered candidates per provider/tier -- these are just a PREFERENCE
# order, not a requirement. If none of them are live, _pick() now falls back
# to picking directly from the live catalog itself (see TIER_HINTS below)
# instead of giving up on the whole provider/tier.
CANDIDATES = {
    "nvidia": {
        "light": [
            "nvidia/llama-3.1-nemotron-nano-8b-v1",
            "nvidia/nemotron-nano-12b-v2-vl",
            "nvidia/nemotron-nano-9b-v2",
            "meta/llama-3.1-8b-instruct",
        ],
        "heavy": [
            "nvidia/nemotron-3-ultra-550b-a55b",
            "nvidia/llama-3.1-nemotron-70b-instruct",
            "meta/llama-3.1-70b-instruct",
        ],
    },
    "mistral": {
        "light": [
            "mistral-small-latest",
            "open-mistral-nemo",
        ],
        "heavy": [
            "mistral-large-latest",
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
# above are live -- lets us pick a real, currently-existing model straight
# from the live catalog by name heuristics, instead of giving up on an
# entire provider tier just because our hand-curated list happens to be
# stale (which free-tier catalogs guarantee will happen periodically).
TIER_HINTS = {
    "light": ["nano", "mini", "small", "8b", "9b", "7b", "4b", "3b", "2b", "1b"],
    "heavy": ["ultra", "large", "70b", "72b", "405b", "550b", "34b", "32b"],
}

# OpenRouter mixes free (":free" suffix) and PAID models in the same
# /models response. Falling back to "any live model" without filtering this
# would risk silently picking a paid model and spending real money -- so
# for openrouter specifically, the live pool is restricted to ":free" ids
# before any heuristic matching happens. NVIDIA/Mistral don't use this
# suffix convention; verify with your account/provider docs whether every
# model returned by their /models endpoint is actually covered under your
# free credits before trusting an unfiltered heuristic pick from them too.
FREE_SUFFIX = ":free"

# Model names containing any of these are disqualified from the "light" tier
# heuristic pick specifically. These indicate extra hidden reasoning/thinking
# tokens generated before every response, which defeats the point of "light"
# being the fast, high-call-volume tier -- Prosecutor/Defender need many
# quick calls, not careful deliberation (that's what "heavy"/Judge is for).
# Confirmed cause of a real ~3min run: the heuristic picked
# "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning" for light tier because
# "nano" hit a size keyword -- but it's a reasoning model, so every single
# Prosecutor/Defender call paid a full reasoning-token tax.
LIGHT_TIER_EXCLUDE = ["reasoning", "thinking", "-r1", "deepseek-r1", "-vl", "-omni"]


def _heuristic_pick_from_live(provider: str, tier: str, live_ids: set) -> str | None:
    pool = live_ids
    if provider == "openrouter":
        pool = {m for m in live_ids if m.endswith(FREE_SUFFIX)}
        if not pool:
            return None  # no free models available at all -- never guess a paid one

    if tier == "light":
        filtered = {m for m in pool if not any(bad in m.lower() for bad in LIGHT_TIER_EXCLUDE)}
        # only apply the exclusion if it doesn't wipe out the whole pool --
        # a slow reasoning model beats having no light-tier model at all
        if filtered:
            pool = filtered

    for hint in TIER_HINTS[tier]:
        matches = sorted(m for m in pool if hint in m.lower())
        if matches:
            return matches[0]

    # no keyword hit at all -- still better to return SOMETHING live
    # (already free/reasoning-filtered as applicable) than nothing
    return sorted(pool)[0] if pool else None


def _fetch_model_ids(provider: str) -> set | None:
    """Returns set of live model ids from provider's /models endpoint, or None on failure."""
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


def _pick(provider: str, tier: str, live_ids: set | None) -> str | None:
    candidates = CANDIDATES[provider][tier]
    for candidate in candidates:
        if live_ids is None or candidate in live_ids:
            return candidate
    if live_ids is None:
        # query itself failed (network/auth/timeout) -- we have no live
        # signal either way, so the hardcoded top candidate is the best
        # guess we have.
        return candidates[0] if candidates else None
    # None of our curated candidates are live -- don't give up on a working
    # provider just because our hand-curated list is stale (expected to
    # happen periodically). Fall back to picking a REAL live model instead,
    # via name heuristics (openrouter is also filtered to ":free" ids only,
    # so this can never accidentally select a paid model there).
    fallback = _heuristic_pick_from_live(provider, tier, live_ids)
    if fallback:
        print(
            f"[model_discovery] NOTE: none of the curated {provider}/{tier} "
            f"candidates {candidates} are live -- CANDIDATES list is stale "
            f"and should be updated. Using live catalog match '{fallback}' for now."
        )
        return fallback
    print(
        f"[model_discovery] WARNING: no usable model found for {provider}/{tier} "
        f"at all -- curated candidates are stale AND no heuristic/free match "
        f"exists in the live catalog. Skipping this provider/tier."
    )
    return None


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
    Returns {provider: {light: model_id, heavy: model_id}} -- same shape
    api_router expects in place of its static ROLE_MODELS placeholder.

    Order of preference: fresh cache (<24h) -> live /models query per
    provider -> stale cache for that provider -> first hardcoded candidate
    (only if the live query itself failed -- see _pick()). Never raises.
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
            tier: _pick(provider, tier, live_ids) for tier in CANDIDATES[provider]
        }

    _save_cache(catalog)
    return catalog