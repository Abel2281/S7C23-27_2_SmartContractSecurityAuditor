"""
orchestrator.py
Ties cache -> courtroom pipeline -> cache write together for one Slither
finding (as produced by code_slicer.slice_all), and loops that over all
findings for a contract. Single-pass (Option A): Prosecutor -> Defender ->
Judge, no re-loops.

REAL finding shape (from code_slicer.py -- NOT the earlier flat-fields guess):
{
  "finding_id": str,
  "check": str,             # Slither detector name
  "impact": str,            # High/Medium/Low
  "confidence": str,        # High/Medium/Low -- separate from impact
  "contract_name": str,
  "related_functions": [
    {
      "name": str,
      "slices": [{"start_line": int, "end_line": int, "code": str}, ...],
      # OR, on a slicing failure: "slices": [], "error": str
    },
    ...
  ]
}
There is NO top-level description/lines_start/lines_end -- one finding can
span multiple functions, each with multiple merged line-span slices (e.g.
reentrancy touching two disjoint blocks). _build_code_context() flattens
this into a single string for the agents, so prosecutor.py/defender.py/
judge.py don't need to know about the multi-function structure at all.

Does NOT touch budget_tracker.py directly -- that's already gated inside
api_router.route() (called by validator.validate_with_retry, called by each
agent). Orchestrator's own jobs: cache check, code-context building, agent
sequencing, failure/degraded-mode aggregation, cache write.
"""

from smart_audit.cache import hash_cache
from smart_audit.agents.prosecutor import prosecute
from smart_audit.agents.defender import defend
from smart_audit.agents.judge import judge
from smart_audit.agents.schemas import (
    DebateRecord,
    ProsecutorCharge,
    DefenderResponse,
    JudgeVerdict,
    Severity,
    Verdict,
)
from smart_audit.router.validator import ValidationFailedError

# re-exported so cli.py only needs to import orchestrator, not api_router too
from smart_audit.router.api_router import AllProvidersExhaustedError  # noqa: F401


def _build_code_context(finding: dict) -> str:
    """
    Flattens a finding's related_functions/slices into one string the agents
    read as "the code". A finding can touch multiple functions; each
    function can have multiple merged line-span slices. Functions that
    failed to slice (missing source, no line spans) are noted rather than
    silently dropped, so the agents know context may be incomplete.
    """
    parts = []
    for fn in finding.get("related_functions", []):
        parts.append(f"### Function: {fn['name']}")
        if fn.get("error"):
            parts.append(f"(slicing error: {fn['error']})")
            continue
        for sl in fn.get("slices", []):
            parts.append(f"Lines {sl['start_line']}-{sl['end_line']}:\n{sl['code']}")
    return "\n\n".join(parts) if parts else "(no code context available)"


def audit_finding(contract_source: str, finding: dict) -> DebateRecord:
    """
    contract_source: FULL contract source text -- must be the exact string
                      the CLI read from disk (same file slither/code_slicer
                      ran against), since hash_cache keys off
                      contract_source + finding_id.
    finding: one entry from code_slicer.slice_all()'s output.

    Cache is checked first; on hit, no agents run and no budget is touched.

    On a Prosecutor/Defender ValidationFailedError (they have no
    fallback_factory of their own), this synthesizes an INCONCLUSIVE
    DebateRecord rather than losing the finding -- keeps with the "never
    crash the CLI on bad model output" principle. Judge's own fallback to
    INCONCLUSIVE happens inside judge.py already and looks the same either way.
    UNLIKE a real verdict, this synthesized record is NOT written to
    hash_cache -- it's a pipeline failure, not a judgment, so a transient
    model hiccup shouldn't permanently freeze the finding at INCONCLUSIVE.
    It'll simply retry the full pipeline on the next run.

    AllProvidersExhaustedError is deliberately NOT caught here -- no
    provider has capacity for ANYTHING at that point, so continuing to the
    next finding would just fail the same way. Propagates to cli.py, which
    decides whether to halt the run.
    """
    finding_id = finding["finding_id"]

    cached = hash_cache.get(contract_source, finding_id)
    if cached is not None:
        return DebateRecord.model_validate(cached)

    code_context = _build_code_context(finding)
    degraded = False

    try:
        charge, meta1 = prosecute(finding, code_context)
        degraded = degraded or meta1["degraded"]

        rebuttal, meta2 = defend(finding, code_context, charge)
        degraded = degraded or meta2["degraded"]

        verdict, meta3 = judge(finding, code_context, charge, rebuttal)
        degraded = degraded or meta3["degraded"]

    except ValidationFailedError as e:
        return _inconclusive_record(finding_id, contract_source, reason=str(e))

    record = DebateRecord(
        finding_id=finding_id,
        contract_hash=hash_cache.compute_hash(contract_source, finding_id),
        prosecutor=charge,
        defender=rebuttal,
        judge=verdict,
        degraded_mode=degraded,
    )
    hash_cache.set(contract_source, finding_id, record.model_dump(mode="json"))
    return record


def audit_contract(contract_source: str, findings: list[dict]) -> list[DebateRecord]:
    """
    findings: code_slicer.slice_all()'s full output list for one contract.

    Runs audit_finding() for each finding in order. Stops immediately and
    lets AllProvidersExhaustedError propagate if it hits -- no point auditing
    finding 5 of 10 if nothing has capacity. cli.py should catch it, print
    the [WARNING] degraded-mode / halt message, and report partial results
    for whatever finished before the halt.
    """
    records: list[DebateRecord] = []
    for finding in findings:
        record = audit_finding(contract_source, finding)
        records.append(record)
    return records


def _inconclusive_record(finding_id: str, contract_source: str, reason: str) -> DebateRecord:
    """Used only when Prosecutor or Defender itself fails outright (schema
    validation exhausted, no fallback_factory). Judge's own INCONCLUSIVE
    fallback is handled inside judge.py and never reaches this function."""
    stub_charge = ProsecutorCharge(
        finding_id=finding_id,
        charge_summary="Prosecutor/Defender stage failed schema validation",
        reasoning=f"Courtroom pipeline could not produce a valid charge/rebuttal: {reason}",
        severity_assessment=Severity.LOW,
        attack_scenario=None,
    )
    stub_rebuttal = DefenderResponse(
        finding_id=finding_id,
        rebuttal_summary="N/A -- pipeline failed upstream of Defender",
        reasoning="Defender stage not reliably completed.",
        concedes=False,
        mitigating_factors=None,
    )
    stub_verdict = JudgeVerdict(
        finding_id=finding_id,
        verdict=Verdict.INCONCLUSIVE,
        final_severity=None,
        reasoning=f"Pipeline failure before/at Defender stage: {reason}",
        patch_recommendation=None,
    )
    return DebateRecord(
        finding_id=finding_id,
        contract_hash=hash_cache.compute_hash(contract_source, finding_id),
        prosecutor=stub_charge,
        defender=stub_rebuttal,
        judge=stub_verdict,
        degraded_mode=True,
    )