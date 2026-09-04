"""
Judge Agent - Phase 3, single-pass (Option A).
Sees the original finding, code, Prosecutor's charge, and Defender's rebuttal.
Delivers the final verdict. Routed to role="judge" (heavy-reasoning tier).

Unlike prosecutor.py/defender.py, this DOES pass a fallback_factory: if the
Judge's own output fails schema validation after retries, we fall back to an
INCONCLUSIVE JudgeVerdict rather than raising -- this is the one place
ARCHITECTURE.md's "safely defaults to INCONCLUSIVE" behavior actually lands.

api_router.AllProvidersExhaustedError is deliberately NOT caught here either
-- validator.py's contract says that's a distinct failure (no capacity
anywhere, not a bad-output problem) and should propagate to the orchestrator,
which is better placed to decide whether to halt the whole run vs. skip.
"""

from smart_audit.router.validator import validate_with_retry
from smart_audit.agents.schemas import (
    ProsecutorCharge,
    DefenderResponse,
    JudgeVerdict,
    Verdict,
)

ROLE = "judge"

SYSTEM_PROMPT = """You are the Judge in a smart contract security courtroom.
You have the original Slither finding, the relevant code, the Prosecutor's
charge, and the Defender's rebuttal. Weigh both arguments against the actual
code -- do not simply defer to whichever side sounded more confident. Decide:
- CONFIRMED: the vulnerability is real and exploitable as charged
- FALSE_POSITIVE: the Defender's mitigating factors hold up; not exploitable
- INCONCLUSIVE: the evidence/arguments don't settle it either way
If CONFIRMED, include a concrete patch recommendation.
Respond ONLY with JSON matching this schema:
{
  "finding_id": str,
  "verdict": "CONFIRMED" | "FALSE_POSITIVE" | "INCONCLUSIVE",
  "final_severity": "High" | "Medium" | "Low" | null,
  "reasoning": str,
  "patch_recommendation": str | null
}
No prose outside the JSON. No markdown fences.
"""


def _build_prompt(
    finding: dict,
    code_slice: str,
    charge: ProsecutorCharge,
    rebuttal: DefenderResponse,
) -> str:
    return (
        f"Finding ID: {finding['id']}\n"
        f"Slither detector: {finding['check']}\n"
        f"Slither-reported severity: {finding['impact']}\n"
        f"Slither description: {finding['description']}\n\n"
        f"Relevant code (lines {finding['lines_start']}-{finding['lines_end']}):\n"
        f"```solidity\n{code_slice}\n```\n\n"
        f"--- PROSECUTOR'S CHARGE ---\n"
        f"Summary: {charge.charge_summary}\n"
        f"Reasoning: {charge.reasoning}\n"
        f"Severity assessment: {charge.severity_assessment.value}\n"
        f"Attack scenario: {charge.attack_scenario or 'none given'}\n\n"
        f"--- DEFENDER'S REBUTTAL ---\n"
        f"Summary: {rebuttal.rebuttal_summary}\n"
        f"Reasoning: {rebuttal.reasoning}\n"
        f"Concedes: {rebuttal.concedes}\n"
        f"Mitigating factors: {rebuttal.mitigating_factors or 'none given'}\n\n"
        f"Deliver the verdict. Use finding_id=\"{finding['id']}\" exactly."
    )


def _inconclusive_fallback(finding_id: str):
    def _factory(last_error: Exception | None) -> JudgeVerdict:
        return JudgeVerdict(
            finding_id=finding_id,
            verdict=Verdict.INCONCLUSIVE,
            final_severity=None,
            reasoning=(
                "Judge output failed schema validation after retries; "
                f"defaulting to INCONCLUSIVE. Last error: {last_error}"
            ),
            patch_recommendation=None,
        )
    return _factory


def judge(
    finding: dict,
    code_slice: str,
    charge: ProsecutorCharge,
    rebuttal: DefenderResponse,
) -> tuple[JudgeVerdict, dict]:
    """
    Returns (verdict, route_meta). route_meta = {provider, model, degraded}.

    Raises:
      api_router.AllProvidersExhaustedError - no provider had capacity at all
                                               (propagates, not caught here)
    Falls back (does not raise) to INCONCLUSIVE on:
      schema validation exhaustion after MAX_RETRIES
    """
    prompt = _build_prompt(finding, code_slice, charge, rebuttal)

    verdict, route_meta = validate_with_retry(
        role=ROLE,
        prompt=prompt,
        schema=JudgeVerdict,
        system=SYSTEM_PROMPT,
        fallback_factory=_inconclusive_fallback(finding["id"]),
    )
    return verdict, route_meta