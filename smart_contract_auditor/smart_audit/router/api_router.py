"""
api_router.py
Stateless routing function: route(role, prompt) -> response.
No shared router object -- budget_tracker.py's SQLite table is the single
source of truth for provider state. Each call re-reads it fresh.

Selection logic (single pass):
  1. Order provider tiers by soft headroom (is_near_limit False first) so we
     PREFER providers with room, avoiding mid-contract 429s.
  2. Within that order, check_and_increment() is the hard atomic gate -- a
     provider is only used if it actually has budget left.
  3. If the provider selected is not the primary tier, or was already near
     its soft threshold, the response is flagged degraded=True so the CLI
     can print the [WARNING] degraded-mode line.

DIAGNOSTIC LOGGING (added post-Phase-3 real run): every attempt -- success
or failure -- now prints role/provider/model/latency/degraded (or the error)
to stdout. This is why a slow real run happens is otherwise invisible: the
returned {content, provider, model, degraded} dict alone doesn't tell you
which providers were tried and skipped, or how long the winning call took.
Cheap to remove later once Rich-based terminal UI (Phase 3 TODO) replaces it.
"""

import os
import time

from . import budget_tracker as bt
from . import model_discovery

PROVIDER_TIERS = ["nvidia", "mistral", "openrouter"]

ROLE_TIER = {
    "prosecutor": "light",
    "defender": "light",
    "judge": "heavy",
}

# lazy, process-local cache -- avoids re-reading the model_catalog.json
# cache file on every single route() call. discover_models() itself has
# its own 24h disk-cache TTL underneath this.
_role_models_cache: dict | None = None


def _get_role_models() -> dict:
    global _role_models_cache
    if _role_models_cache is None:
        _role_models_cache = model_discovery.discover_models()
    return _role_models_cache


def refresh_models() -> dict:
    """Forces a live re-query of all provider /models endpoints. Call after
    a dispatch fails with a 'model not found'-type error, or on demand."""
    global _role_models_cache
    _role_models_cache = model_discovery.discover_models(force_refresh=True)
    return _role_models_cache

ENDPOINTS = {
    "nvidia": "https://integrate.api.nvidia.com/v1/chat/completions",
    "mistral": "https://api.mistral.ai/v1/chat/completions",
    "openrouter": "https://openrouter.ai/api/v1/chat/completions",
}

API_KEY_ENV = {
    "nvidia": "NVIDIA_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}


class AllProvidersExhaustedError(RuntimeError):
    """Raised when every configured provider is either out of budget or failed."""


def _api_key(provider: str) -> str | None:
    return os.environ.get(API_KEY_ENV[provider])


def _dispatch(provider: str, model: str, prompt: str, system: str | None) -> str:
    """Fires the actual HTTP call. Raises on network/HTTP error -- caller catches."""
    import requests

    key = _api_key(provider)
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    resp = requests.post(
        ENDPOINTS[provider],
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={"model": model, "messages": messages},
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"]


def route(role: str, prompt: str, system: str | None = None) -> dict:
    """
    Routes a single prompt for the given agent role through the tiered
    provider hierarchy. Returns:
      {content, provider, model, degraded}
    Raises AllProvidersExhaustedError if nothing could serve the request.
    """
    if role not in ROLE_TIER:
        raise ValueError(f"unknown role: {role}")
    tier = ROLE_TIER[role]

    est_tokens = bt.estimate_tokens(prompt)
    if system:
        est_tokens += bt.estimate_tokens(system)

    # prefer providers with soft headroom; is_near_limit False sorts first
    ordered = sorted(PROVIDER_TIERS, key=lambda p: bt.is_near_limit(p))

    last_error: Exception | None = None

    for provider in ordered:
        if not _api_key(provider):
            print(f"[api_router] role={role} provider={provider}: SKIP (no API key configured)")
            continue  # not configured, skip silently

        was_near_limit = bt.is_near_limit(provider)

        if not bt.check_and_increment(provider, est_tokens):
            print(f"[api_router] role={role} provider={provider}: SKIP (hard budget cap reached)")
            continue  # hard cap hit, try next tier

        model = _get_role_models().get(provider, {}).get(tier)
        if model is None:
            print(f"[api_router] role={role} provider={provider}/{tier}: SKIP (no usable model from discovery)")
            continue  # discovery found nothing usable for this provider/tier
        degraded = was_near_limit or provider != PROVIDER_TIERS[0]

        start = time.monotonic()
        try:
            content = _dispatch(provider, model, prompt, system)
            elapsed = time.monotonic() - start
            print(
                f"[api_router] role={role} provider={provider} model={model} "
                f"latency={elapsed:.1f}s degraded={degraded} -> SUCCESS"
            )
            return {
                "content": content,
                "provider": provider,
                "model": model,
                "degraded": degraded,
            }
        except Exception as e:  # noqa: BLE001 -- deliberately broad, we fall through tiers
            elapsed = time.monotonic() - start
            print(
                f"[api_router] role={role} provider={provider} model={model} "
                f"latency={elapsed:.1f}s -> FAILED: {type(e).__name__}: {e}"
            )
            last_error = e
            continue

    raise AllProvidersExhaustedError(
        f"all providers exhausted or failed for role='{role}'. last_error={last_error}"
    )