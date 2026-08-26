"""
validator.py
Schema validation + bounded retry wrapper around api_router.route(). Parses
agent response content as JSON, validates against a Pydantic schema, and on
failure re-dispatches with the validation error appended as correction
context -- up to MAX_RETRIES times. If still failing, falls back to a
caller-supplied default (e.g. an INCONCLUSIVE verdict) instead of crashing.

schema-agnostic by design: agents/schemas.py (phase 3) will define the real
Prosecutor/Defender/Judge Pydantic models. This module takes any BaseModel
subclass, so it doesn't need to change when those land.
"""

import json
from typing import Callable, Type, TypeVar
from pydantic import BaseModel, ValidationError
from . import api_router

T = TypeVar("T", bound=BaseModel)
MAX_RETRIES = 2

class ValidationFailedError(RuntimeError):
    """Raised when retries are exhausted and no fallback_factory was given."""


def _extract_json(text: str) -> dict:
    """Strips markdown code fences if present, parses JSON. Raises json.JSONDecodeError."""
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        stripped = "\n".join(lines)
    return json.loads(stripped)


def validate_with_retry(
    role: str,
    prompt: str,
    schema: Type[T],
    system: str | None = None,
    fallback_factory: Callable[[Exception | None], T] | None = None,
    max_retries: int = MAX_RETRIES,
) -> tuple[T, dict]:
    """
    Routes `prompt` for `role` via api_router, parses the response as JSON,
    validates against `schema`. On JSON-decode or Pydantic validation errors,
    re-dispatches with the error fed back to the model as correction context,
    up to `max_retries` additional attempts (each a real, budget-consuming
    API call -- correction round-trips are not free).

    On exhaustion:
      - fallback_factory given  -> returns fallback_factory(last_error)
      - fallback_factory absent -> raises ValidationFailedError

    Returns (validated_instance, route_meta) where route_meta is the
    {provider, model, degraded} dict from the LAST api_router call made
    (even when falling back, so the caller can still log/display which
    provider/model produced the failing output).

    Does not catch api_router.AllProvidersExhaustedError -- that's a
    distinct failure mode (no capacity anywhere) and should propagate.
    """
    last_error: Exception | None = None
    route_meta: dict = {}
    current_prompt = prompt

    for attempt in range(max_retries + 1):
        result = api_router.route(role, current_prompt, system=system)
        route_meta = {
            "provider": result["provider"],
            "model": result["model"],
            "degraded": result["degraded"],
        }
        try:
            parsed = _extract_json(result["content"])
            validated = schema.model_validate(parsed)
            return validated, route_meta
        except (json.JSONDecodeError, ValidationError) as e:
            last_error = e
            if attempt < max_retries:
                current_prompt = (
                    f"{prompt}\n\n"
                    f"--- CORRECTION NEEDED ---\n"
                    f"Your previous response failed schema validation:\n{e}\n\n"
                    f"Respond again with ONLY valid JSON matching the required "
                    f"schema. No prose, no markdown code fences."
                )
            continue

    if fallback_factory is not None:
        return fallback_factory(last_error), route_meta

    raise ValidationFailedError(
        f"schema validation failed after {max_retries} retries for role='{role}'. "
        f"last_error={last_error}"
    )