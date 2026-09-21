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

Every attempt -- success or failure -- prints role/provider/model/latency/
degraded (or the error) to stdout, since the returned dict alone doesn't
show which providers were tried/skipped or how long the winning call took.
"""

import os
import time
from . import budget_tracker as bt
from . import model_discovery

ROLE_TIER = {
    "prosecutor": "light",
    "defender": "light",
    "judge": "heavy",
}

TIER_PROVIDER_ORDER = {
    "light": ["groq", "gemini", "nvidia"],
    "heavy": ["nvidia", "groq", "gemini"],
}

TIER_TIMEOUT = {
    "light": 25,
    "heavy": 40,
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
    "groq": "https://api.groq.com/openai/v1/chat/completions",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
}

API_KEY_ENV = {
    "nvidia": "NVIDIA_API_KEY",
    "groq": "GROQ_API_KEY",
    "gemini": "GEMINI_API_KEY",
}


class AllProvidersExhaustedError(RuntimeError):
    """Raised when every configured provider is either out of budget or failed."""


# process-local, resets on next CLI invocation. Once a (provider, model)
# pair fails a real dispatch this run, it's skipped for the rest of the run
# rather than retried into the same wall on every subsequent role call.
# Deliberately NOT persisted -- a bad call today (timeout, transient
# overload) shouldn't permanently blacklist a model discovery confirmed live.
_failed_this_run: set[tuple[str, str]] = set()


def _api_key(provider: str) -> str | None:
    return os.environ.get(API_KEY_ENV[provider])


def _dispatch(provider: str, model: str, prompt: str, system: str | None, timeout: int) -> str:
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
        timeout=timeout,
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
    timeout = TIER_TIMEOUT[tier]

    est_tokens = bt.estimate_tokens(prompt)
    if system:
        est_tokens += bt.estimate_tokens(system)

    # resolve candidate models per provider first -- budget limits are keyed
    # by (provider, model), so we need the model in hand before we can check
    # or sort by headroom. Each provider now offers a priority-ordered LIST
    # of validated candidates, not just one -- so a single bad model falls
    # through to the next candidate on the SAME provider before hopping to
    # an entirely different provider tier.
    role_models = _get_role_models()
    provider_order = TIER_PROVIDER_ORDER[tier]
    candidates: list[tuple[str, str]] = []
    for provider in provider_order:
        models = role_models.get(provider, {}).get(tier, [])
        if not models:
            print(f"[api_router] role={role} provider={provider}/{tier}: SKIP (no usable model from discovery)")
            continue
        for model in models:
            if (provider, model) in _failed_this_run:
                print(f"[api_router] role={role} provider={provider} model={model}: SKIP (failed earlier this run)")
                continue
            candidates.append((provider, model))

    # prefer (provider, model) pairs with soft headroom; is_near_limit False
    # sorts first. Stable sort preserves provider-then-candidate priority
    # order among pairs with the same near_limit status.
    ordered = sorted(candidates, key=lambda pm: bt.is_near_limit(pm[0], pm[1]))

    last_error: Exception | None = None

    for provider, model in ordered:
        if not _api_key(provider):
            print(f"[api_router] role={role} provider={provider}: SKIP (no API key configured)")
            continue  # not configured, skip silently

        was_near_limit = bt.is_near_limit(provider, model)

        if not bt.check_and_increment(provider, model, est_tokens):
            print(f"[api_router] role={role} provider={provider} model={model}: SKIP (hard budget cap reached)")
            continue  # hard cap hit, try next candidate

        degraded = was_near_limit or provider != provider_order[0]

        start = time.monotonic()
        try:
            content = _dispatch(provider, model, prompt, system, timeout)
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
            _failed_this_run.add((provider, model))
            last_error = e
            continue

    raise AllProvidersExhaustedError(
        f"all providers exhausted or failed for role='{role}'. last_error={last_error}"
    )