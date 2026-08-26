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
"""

import json
import os
import time
from pathlib import Path

CACHE_PATH = Path("output") / "model_catalog.json"
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

# priority-ordered candidates per provider/tier -- first one found live wins.
# hand-curated; discovery verifies + selects, doesn't invent ids.
CANDIDATES = {
    "nvidia": {
        "light": [
            "nvidia/nemotron-nano-30b",
            "nvidia/nemotron-nano-9b",
            "meta/llama-3.1-8b-instruct",
        ],
        "heavy": [
            "nvidia/nemotron-ultra-550b",
            "nvidia/nemotron-super-49b",
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
            "meta-llama/llama-3.1-8b-instruct:free",
            "google/gemma-2-9b-it:free",
            "mistralai/mistral-7b-instruct:free",
        ],
        "heavy": [
            "meta-llama/llama-3.1-70b-instruct:free",
            "google/gemma-2-27b-it:free",
            "nousresearch/hermes-3-llama-3.1-405b:free",
        ],
    },
}


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
    # nothing matched live catalog -- fall back to top candidate anyway;
    # api_router's dispatch will surface the real error if it's truly gone
    return candidates[0] if candidates else None


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
    provider -> stale cache for that provider -> first hardcoded candidate.
    Never raises.
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