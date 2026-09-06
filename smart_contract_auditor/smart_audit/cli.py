"""
cli.py
Terminal entry point for ChainGuard.

Usage:
    smart-audit run contracts/Vulnerabilities.sol
    smart-audit run ./Vulnerabilities.sol
    smart-audit run Vulnerabilities.sol          # bare filename -> looked up in contracts/

Flow (Phase 1 in-memory, one disk write; Phase 3 courtroom, one more disk write):
    resolve path -> slither_runner.run_slither -> json_filter.filter_findings
    -> code_slicer.slice_all -> write output/<contract_stem>_phase1.json
    -> orchestrator.audit_contract (Prosecutor -> Defender -> Judge per finding,
       cache-gated) -> write output/<contract_stem>_debate_records.json

The debate-records write is a stand-in for Phase 4's real report_generator.py
(a Markdown report) -- raw JSON for now so results are actually inspectable
before that module exists.
"""

import json
from pathlib import Path

import typer
from typing_extensions import Annotated
from typing import Optional
from smart_audit.preprocessor.slither_runner import resolve_target_path, run_slither
from smart_audit.preprocessor.json_filter import filter_findings
from smart_audit.preprocessor.code_slicer import slice_all
from smart_audit import orchestrator
from smart_audit.router.api_router import AllProvidersExhaustedError
from smart_audit.agents.schemas import Verdict

from smart_audit.utils.terminal_ui import (
    print_banner,
    print_info,
    print_success,
    print_warning,
    print_error,
)

app = typer.Typer(
    name="smart-audit",
    help="Multi-Agent Smart Contract Security Auditor CLI",
    add_completion=False,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = PROJECT_ROOT / "output"


@app.callback()
def main():
    """Multi-Agent Smart Contract Security Auditor CLI"""
    pass


def _write_phase1_output(sliced: list[dict], contract_path: Path) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUTPUT_DIR / f"{contract_path.stem}_phase1.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(sliced, f, indent=2)
    return out_path


def _write_debate_records(records: list, contract_path: Path) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUTPUT_DIR / f"{contract_path.stem}_debate_records.json"
    payload = [r.model_dump(mode="json") for r in records]
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    return out_path


def _print_courtroom_summary(records: list) -> None:
    confirmed = sum(1 for r in records if r.judge.verdict == Verdict.CONFIRMED)
    false_positive = sum(1 for r in records if r.judge.verdict == Verdict.FALSE_POSITIVE)
    inconclusive = sum(1 for r in records if r.judge.verdict == Verdict.INCONCLUSIVE)
    degraded_count = sum(1 for r in records if r.degraded_mode)

    print_success(
        f"Courtroom complete: {confirmed} confirmed, "
        f"{false_positive} false positive, {inconclusive} inconclusive"
    )
    if degraded_count:
        print_warning(
            f"{degraded_count}/{len(records)} finding(s) ran in degraded mode "
            f"(fallback provider/model, or a synthesized fallback verdict)."
        )


@app.command(name="run")
def run_cmd(
    target: Annotated[
        str,
        typer.Argument(help="Path to the target .sol file (bare filename, relative, or absolute)."),
    ],
    max_findings: Annotated[
        Optional[int],
        typer.Option(help="Cap number of findings kept after filtering."),
    ] = None,
    fast: Annotated[
        bool,
        typer.Option(
            "--fast",
            "-f",
            help="Reserved for future backoff/delay buffers between agent calls. No-op today.",
        ),
    ] = False,
    # TODO (future): a --skip-courtroom flag to run Phase 1 only, no API keys/
    # budget touched. Deliberately not adding now -- keeping the CLI surface
    # minimal until there's an actual need (e.g. CI runs that shouldn't burn
    # LLM budget). Revisit post-Phase-3.
):
    """
    Run static analysis, pre-processing, and (unless skipped) the multi-agent
    courtroom pipeline against a Solidity contract.
    """
    print_banner()

    # 1. Resolve + validate path (bare filename / relative / absolute, with contracts/ fallback)
    contract_path = resolve_target_path(target)
    if contract_path is None:
        print_error(f"Target path could not be resolved: '{target}'")
        raise typer.Exit(code=1)

    if contract_path.suffix != ".sol":
        print_error(f"Invalid file extension '{contract_path.suffix}'. Target must be a '.sol' file.")
        raise typer.Exit(code=1)

    print_success(f"Validated target path: {contract_path.name}")

    if fast:
        print_warning("Fast mode flag set (no-op until delay buffers exist).")

    # 2. Slither analysis (in-memory dict, scratch json deleted internally)
    print_info("[1/3] Running Slither analysis...")
    raw_data = run_slither(contract_path)

    if not raw_data:
        print_error("Slither analysis failed or generated no output. Check if Slither and solc are installed.")
        raise typer.Exit(code=1)

    detector_count = len(raw_data.get("results", {}).get("detectors", []))
    print_success(f"{detector_count} findings detected")

    # 3. Filtering (in-memory, no disk I/O)
    print_info("[2/3] Filtering High/Medium findings...")
    filtered_findings = filter_findings(raw_data, max_findings=max_findings)
    print_success(f"{len(filtered_findings)} findings retained")

    # 4. AST code slicing (in-memory, no disk I/O)
    print_info("[3/3] Extracting AST function code slices...")
    sliced_findings = slice_all(filtered_findings, contract_path)

    # 5. Single end-of-phase-1 write
    phase1_path = _write_phase1_output(sliced_findings, contract_path)
    print_success(f"Phase 1 pre-processing complete -> {phase1_path.name}.")

    if not sliced_findings:
        print_info("No findings survived filtering -- nothing for the courtroom to review.")
        return

    # 6. Courtroom pipeline: Prosecutor -> Defender -> Judge, per finding, cache-gated.
    # contract_source is read fresh here (not reassembled from code_slicer's
    # line-numbered slices) so hash_cache keys off the exact same bytes
    # slither/code_slicer already ran against.
    print_info(
        f"Running courtroom pipeline on {len(sliced_findings)} finding(s) "
        f"(Prosecutor -> Defender -> Judge)..."
    )
    contract_source = contract_path.read_text(encoding="utf-8", errors="replace")

    try:
        records = orchestrator.audit_contract(contract_source, sliced_findings)
    except AllProvidersExhaustedError as e:
        print_error(
            f"All LLM providers exhausted -- halting courtroom pipeline partway through: {e}"
        )
        raise typer.Exit(code=1)

    _print_courtroom_summary(records)

    # 7. Single end-of-phase-3 write (stand-in for Phase 4's report_generator.py)
    debate_path = _write_debate_records(records, contract_path)
    print_success(f"Debate records written -> {debate_path.name}.")


if __name__ == "__main__":
    app()