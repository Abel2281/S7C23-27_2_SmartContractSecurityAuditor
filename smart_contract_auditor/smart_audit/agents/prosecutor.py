"""
Prosecutor Agent - Phase 3, single-pass (Option A).
Takes one finding from code_slicer.slice_all() + its flattened code context,
produces a ProsecutorCharge.

Routing + schema validation + bounded retry all happen inside
validate_with_retry() -- this module just builds the prompt and unpacks the
result. No direct api_router calls here.

No fallback_factory is passed: if the Prosecutor can't produce a valid charge
after retries, ValidationFailedError propagates. INCONCLUSIVE is a Judge-level
concept (per schemas.py / ARCHITECTURE.md); the orchestrator decides how to
handle a Prosecutor failure, not this module.
"""

from smart_audit.router.validator import validate_with_retry
from smart_audit.agents.schemas import ProsecutorCharge

ROLE = "prosecutor"

SYSTEM_PROMPT = """You are the Prosecutor in a smart contract security courtroom.
Given a Slither static-analysis finding and the relevant Solidity code, argue
that the vulnerability is real and exploitable. Be specific: cite line
numbers, state variables, and a concrete attack path where possible. If the
finding touches multiple functions, consider how they interact.
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


def _build_prompt(finding: dict, code_context: str) -> str:
    return (
        f"Finding ID: {finding['finding_id']}\n"
        f"Slither detector: {finding['check']}\n"
        f"Slither-reported impact: {finding['impact']}\n"
        f"Slither-reported confidence: {finding['confidence']}\n"
        f"Contract: {finding['contract_name']}\n\n"
        f"Relevant code:\n{code_context}\n\n"
        f"Formulate the charge. Use finding_id=\"{finding['finding_id']}\" exactly."
    )


def prosecute(finding: dict, code_context: str) -> tuple[ProsecutorCharge, dict]:
    """
    finding: one entry from code_slicer.slice_all()'s output. Expected keys:
             finding_id, check, impact, confidence, contract_name,
             related_functions
    code_context: flattened code string built by orchestrator._build_code_context()

    Returns (charge, route_meta). route_meta = {provider, model, degraded} --
    caller (orchestrator) should OR this into the DebateRecord.degraded_mode
    flag alongside Defender's and Judge's route_meta.

    Raises:
      ValidationFailedError            - schema validation exhausted retries
      api_router.AllProvidersExhaustedError - no provider had capacity at all
    """
    prompt = _build_prompt(finding, code_context)

    charge, route_meta = validate_with_retry(
        role=ROLE,
        prompt=prompt,
        schema=ProsecutorCharge,
        system=SYSTEM_PROMPT,
    )
    return charge, route_meta