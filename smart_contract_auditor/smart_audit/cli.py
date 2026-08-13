"""
cli.py
Terminal entry point for ChainGuard Phase 1 (preprocessing pipeline).

Usage:
    smart-audit run contracts/Vulnerabilities.sol
    smart-audit run ./Vulnerabilities.sol
    smart-audit run Vulnerabilities.sol          # bare filename -> looked up in contracts/

Flow (all in-memory, one disk write at the end):
    resolve path -> slither_runner.run_slither -> json_filter.filter_findings
    -> code_slicer.slice_all -> write output/<contract_stem>_phase1.json
"""

import json
from pathlib import Path

import typer
from typing_extensions import Annotated
from typing import Optional
from smart_audit.preprocessor.slither_runner import resolve_target_path, run_slither
from smart_audit.preprocessor.json_filter import filter_findings
from smart_audit.preprocessor.code_slicer import slice_all

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
            help="Reserved for future backoff/delay buffers between agent calls (Phase 2). No-op today.",
        ),
    ] = False,
):
    """
    Run static analysis and pre-processing against a Solidity contract.
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
        print_warning("Fast mode flag set (no-op until Phase 2 delay buffers exist).")

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

    # 5. Single end-of-phase write
    out_path = _write_phase1_output(sliced_findings, contract_path)
    print_success(f"Phase 1 pre-processing complete -> {out_path.name}. Ready for Phase 2.")


if __name__ == "__main__":
    app()