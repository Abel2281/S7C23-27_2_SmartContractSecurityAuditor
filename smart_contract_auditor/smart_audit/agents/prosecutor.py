"""
Prosecutor Agent - Phase 3, single-pass (Option A).
Takes one filtered Slither finding + its code slice, produces a ProsecutorCharge.

Routing + schema validation + bounded retry all happen inside
validator.validate_with_retry() -- this module just builds the prompt and
unpacks the result. No direct api_router calls here.

No fallback_factory is passed: if the Prosecutor can't produce a valid charge
after retries, ValidationFailedError propagates. INCONCLUSIVE is a Judge-level
concept (per schemas.py / ARCHITECTURE.md); the orchestrator decides how to
handle a Prosecutor failure, not this module.
"""

from smart_audit.router.validator import validate_with_retry
from smart_audit.agents.schemas import ProsecutorCharge

ROLE = "prosecutor"

SYSTEM_PROMPT = """You are the Prosecutor in a smart contract security courtroom.
Given a Slither static-analysis finding and the relevant Solidity code slice,
argue that the vulnerability is real and exploitable. Be specific: cite line
numbers, state variables, and a concrete attack path where possible.
Respond ONLY with JSON matching this schema:
{
  "finding_id": str,
  "charge_summary": str,
  "reasoning": str,
  "severity_assessment": "High" | "Medium" | "Low",
  "attack_scenario": str | null
}
No prose outside the JSON. No markdown fences.
"""


def _build_prompt(finding: dict, code_slice: str) -> str:
    return (
        f"Finding ID: {finding['id']}\n"
        f"Slither detector: {finding['check']}\n"
        f"Slither-reported severity: {finding['impact']}\n"
        f"Slither description: {finding['description']}\n\n"
        f"Relevant code (lines {finding['lines_start']}-{finding['lines_end']}):\n"
        f"```solidity\n{code_slice}\n```\n\n"
        f"Formulate the charge. Use finding_id=\"{finding['id']}\" exactly."
    )


def prosecute(finding: dict, code_slice: str) -> tuple[ProsecutorCharge, dict]:
    """
    finding: filtered Slither finding dict (post json_filter.py). Expected keys:
             id, check, impact, description, lines_start, lines_end
    code_slice: source snippet from code_slicer.py for this finding

    Returns (charge, route_meta). route_meta = {provider, model, degraded} --
    caller (orchestrator) should OR this into the DebateRecord.degraded_mode
    flag alongside Defender's and Judge's route_meta.

    Raises:
      ValidationFailedError            - schema validation exhausted retries
      api_router.AllProvidersExhaustedError - no provider had capacity at all
    """
    prompt = _build_prompt(finding, code_slice)

    charge, route_meta = validate_with_retry(
        role=ROLE,
        prompt=prompt,
        schema=ProsecutorCharge,
        system=SYSTEM_PROMPT,
    )
    return charge, route_meta