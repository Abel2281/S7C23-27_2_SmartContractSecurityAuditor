"""
orchestrator.py
Ties cache -> courtroom pipeline -> cache write together for one Slither
finding, and loops that over all findings for a contract. Single-pass
(Option A): Prosecutor -> Defender -> Judge, no re-loops.

Does NOT touch budget_tracker.py directly -- headroom checks already happen
inside api_router.route(), called by validator.validate_with_retry(), called
by each agent. Orchestrator's job is just: cache check, sequencing, failure
handling, cache write.
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


def audit_finding(contract_source: str, finding: dict, code_slice: str) -> DebateRecord:
    """
    contract_source: FULL contract source text -- must be the exact string
                      json_filter.py / code_slicer.py operated on, since
                      hash_cache keys off contract_source + finding_id.
    finding: filtered Slither finding dict (post json_filter.py), needs 'id'
    code_slice: code_slicer.py's output for this finding

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
    finding_id = finding["id"]

    cached = hash_cache.get(contract_source, finding_id)
    if cached is not None:
        return DebateRecord.model_validate(cached)

    degraded = False

    try:
        charge, meta1 = prosecute(finding, code_slice)
        degraded = degraded or meta1["degraded"]

        rebuttal, meta2 = defend(finding, code_slice, charge)
        degraded = degraded or meta2["degraded"]

        verdict, meta3 = judge(finding, code_slice, charge, rebuttal)
        degraded = degraded or meta3["degraded"]

    except ValidationFailedError as e:
        # NOT cached: this is a pipeline failure, not a real verdict. Caching
        # it would permanently stick the finding at INCONCLUSIVE even after
        # a transient model/provider hiccup clears up on the next run.
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


def audit_contract(
    contract_source: str, findings: list[dict], code_slices: dict[str, str]
) -> list[DebateRecord]:
    """
    findings: list of filtered Slither finding dicts (post json_filter.py)
    code_slices: {finding_id: code_slice} from code_slicer.py

    Runs audit_finding() for each finding in order. Stops immediately and
    lets AllProvidersExhaustedError propagate if it hits -- no point auditing
    finding 5 of 10 if nothing has capacity. cli.py should catch it, print
    the [WARNING] degraded-mode / halt message, and report partial results
    for whatever finished before the halt.
    """
    records: list[DebateRecord] = []
    for finding in findings:
        code_slice = code_slices[finding["id"]]
        record = audit_finding(contract_source, finding, code_slice)
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