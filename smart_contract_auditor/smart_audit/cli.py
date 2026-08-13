from pathlib import Path
import typer
from typing_extensions import Annotated

# Preprocessor imports
from smart_audit.preprocessor.slither_runner import run_slither
from smart_audit.preprocessor.json_filter import filter_findings, save_findings
from smart_audit.preprocessor.code_slicer import slice_all, save_slices

# UI imports
from smart_audit.utils.terminal_ui import (
    console,
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


@app.callback()
def main():
    """Multi-Agent Smart Contract Security Auditor CLI"""
    pass


@app.command(name="run")
def run_cmd(
    target: Annotated[
        Path,
        typer.Argument(
            help="Path to the target .sol file or project directory.",
        ),
    ],
    fast: Annotated[
        bool,
        typer.Option(
            "--fast",
            "-f",
            help="Bypass execution delays between analysis steps.",
        ),
    ] = False,
):
    """
    Run static analysis and pre-processing against a Solidity contract.
    """
    print_banner()

    # 1. Validation
    if not target.exists():
        print_error(f"Target path does not exist: '{target}'")
        raise typer.Exit(code=1)

    if target.is_file() and target.suffix != ".sol":
        print_error(f"Invalid file extension '{target.suffix}'. Target must be a '.sol' file.")
        raise typer.Exit(code=1)

    print_success(f"Validated target path: {target.name}")

    if fast:
        print_warning("Fast mode enabled.")

    print_info("Starting Phase 1 pre-processing pipeline...")

    # 2. Slither Analysis
    with console.status("[bold green]Executing Slither analysis...", spinner="dots"):
        raw_data = run_slither(str(target), output_json_path="output_raw.json")

    if not raw_data:
        print_error("Slither analysis failed or generated no output. Check if Slither and solc are installed.")
        raise typer.Exit(code=1)

    raw_results = raw_data.get("results", {}).get("detectors", [])
    raw_count = len(raw_results)
    print_success(f"Slither analysis complete ({raw_count} raw detectors triggered).")

    # 3. JSON Filtering
    with console.status("[bold green]Filtering High/Medium findings...", spinner="dots"):
        filtered_findings = filter_findings(raw_data)
        saved_filter_path = save_findings(filtered_findings, "filtered_findings.json")

    filtered_count = len(filtered_findings)
    print_success(
        f"JSON Filtering complete. Retained {filtered_count} High/Medium findings -> {saved_filter_path.name}"
    )

    # 4. AST Code Slicing
    with console.status("[bold green]Extracting AST function code slices...", spinner="dots"):
        sliced_findings = slice_all(filtered_findings)
        save_slices(sliced_findings)

    print_success(
        f"AST Slicing complete. Context extracted for {len(sliced_findings)} findings -> sliced_findings.json"
    )

    # 5. Final Handoff Output
    print_success("Phase 1 pre-processing complete. Ready for Phase 2.")


if __name__ == "__main__":
    app()