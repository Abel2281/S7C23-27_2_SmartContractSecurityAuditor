"""
Defender Agent - Phase 3, single-pass (Option A).
Takes the Prosecutor's charge (+ original finding/code context) and produces
a DefenderResponse arguing why it may be a false positive.

Same pattern as prosecutor.py: validate_with_retry owns routing/retry.
No fallback_factory -- failure propagates to orchestrator.
"""

from smart_audit.router.validator import validate_with_retry
from smart_audit.agents.schemas import ProsecutorCharge, DefenderResponse

ROLE = "defender"

SYSTEM_PROMPT = """You are the Defender in a smart contract security courtroom.
The Prosecutor has charged the code below with a vulnerability. Your job is
to scrutinize the charge -- check for guard conditions, access-control
modifiers, invariants, or state constraints the Prosecutor may have missed
that would prevent the exploit. Argue in good faith: if the charge holds up
under scrutiny, concede it. Do not concede just to be agreeable, and do not
reject just to be adversarial.
Respond ONLY with JSON matching this schema:
{
  "finding_id": str,
  "rebuttal_summary": str,
  "reasoning": str,
  "concedes": bool,
  "mitigating_factors": str | null
}
No prose outside the JSON. No markdown fences.
"""


def _build_prompt(finding: dict, code_context: str, charge: ProsecutorCharge) -> str:
    return (
        f"Finding ID: {finding['finding_id']}\n"
        f"Slither detector: {finding['check']}\n"
        f"Slither-reported impact: {finding['impact']}\n"
        f"Slither-reported confidence: {finding['confidence']}\n"
        f"Contract: {finding['contract_name']}\n\n"
        f"Relevant code:\n{code_context}\n\n"
        f"--- PROSECUTOR'S CHARGE ---\n"
        f"Summary: {charge.charge_summary}\n"
        f"Reasoning: {charge.reasoning}\n"
        f"Severity assessment: {charge.severity_assessment.value}\n"
        f"Attack scenario: {charge.attack_scenario or 'none given'}\n\n"
        f"Formulate the defense. Use finding_id=\"{finding['finding_id']}\" exactly."
    )


def defend(
    finding: dict, code_context: str, charge: ProsecutorCharge
) -> tuple[DefenderResponse, dict]:
    """
    finding: one entry from code_slicer.slice_all()'s output
    code_context: flattened code string built by orchestrator._build_code_context()
    charge: the ProsecutorCharge already produced for this finding

    Returns (response, route_meta) -- same route_meta shape as prosecutor.py's
    prosecute(): {provider, model, degraded}.

    Raises:
      ValidationFailedError                 - schema validation exhausted retries
      api_router.AllProvidersExhaustedError - no provider had capacity at all
    """
    prompt = _build_prompt(finding, code_context, charge)

    response, route_meta = validate_with_retry(
        role=ROLE,
        prompt=prompt,
        schema=DefenderResponse,
        system=SYSTEM_PROMPT,
    )
    return response, route_meta