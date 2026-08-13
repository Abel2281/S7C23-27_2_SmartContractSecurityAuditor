"""
code_slicer.py
Rule-based code slicer. No LLM calls.

Input : filtered findings (in-memory list, from json_filter.filter_findings)
        + the resolved contract Path (same file the whole pipeline is running on)
Output: sliced findings (in-memory list) — cli.py handles the single
        end-of-phase disk write, not this module.

For each finding, for each related_function, merges that function's
distinct line spans (padded with CONTEXT_PADDING lines) into non-overlapping
blocks, then pulls the exact source text for each block. Per-function spans
are kept separate (not collapsed across functions) per existing span-handling
rule.
"""

from pathlib import Path

CONTEXT_PADDING = 3  # lines of context before/after each span


def load_source_lines(contract_path: Path) -> list[str] | None:
    """Read the single contract file the CLI resolved at startup. Whole
    pipeline runs against one user-supplied .sol file now, so no basename
    lookup against a contracts/ folder is needed anymore."""
    contract_path = Path(contract_path)
    if not contract_path.exists():
        return None

    with open(contract_path, "r", encoding="utf-8", errors="replace") as f:
        return f.read().splitlines()


def merge_spans(spans: list[dict], padding: int, max_line: int) -> list[tuple[int, int]]:
    """
    Pad each (start_line, end_line) span, clamp to file bounds, then merge
    any spans that overlap or touch after padding. Returns sorted list of
    (start, end) tuples, 1-indexed inclusive.
    """
    padded = []
    for span in spans:
        start = max(1, span["start_line"] - padding)
        end = min(max_line, span["end_line"] + padding)
        padded.append((start, end))

    padded.sort(key=lambda s: s[0])

    merged: list[list[int]] = []
    for start, end in padded:
        if merged and start <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])

    return [(s, e) for s, e in merged]


def extract_code_block(source_lines: list[str], start: int, end: int) -> str:
    """Extract lines [start, end] (1-indexed, inclusive), prefixed with line numbers."""
    block_lines = []
    for line_no in range(start, end + 1):
        text = source_lines[line_no - 1] if 0 <= line_no - 1 < len(source_lines) else ""
        block_lines.append(f"{line_no}: {text}")
    return "\n".join(block_lines)


def slice_finding(finding: dict, source_lines: list[str] | None) -> dict:
    sliced_functions = []
    for related_fn in finding.get("related_functions", []):
        fn_name = related_fn["name"]
        spans = related_fn.get("lines", [])

        if source_lines is None or not spans:
            sliced_functions.append({
                "name": fn_name,
                "slices": [],
                "error": "source file not found" if source_lines is None else "no line spans",
            })
            continue

        merged = merge_spans(spans, CONTEXT_PADDING, len(source_lines))

        slices = []
        for start, end in merged:
            slices.append({
                "start_line": start,
                "end_line": end,
                "code": extract_code_block(source_lines, start, end),
            })

        sliced_functions.append({
            "name": fn_name,
            "slices": slices,
        })

    return {
        "finding_id": finding["finding_id"],
        "check": finding["check"],
        "impact": finding["impact"],
        "confidence": finding["confidence"],
        "contract_name": finding["contract_name"],
        "related_functions": sliced_functions,
    }


def slice_all(findings: list[dict], contract_path: Path) -> list[dict]:
    """contract_path = the single .sol file resolved once at CLI startup,
    same file slither/json_filter already ran against."""
    source_lines = load_source_lines(contract_path)
    return [slice_finding(f, source_lines) for f in findings]


if __name__ == "__main__":
    # standalone dev test — adjust to a real contract path under contracts/
    import json

    PROJECT_ROOT = Path(__file__).resolve().parents[2]
    test_contract = PROJECT_ROOT / "contracts" / "0x01f8c4e3fa3edeb29e514cba738d87ce8c091d3f.sol"
    test_findings_path = PROJECT_ROOT / "output" / "filtered_findings.json"

    if test_contract.exists() and test_findings_path.exists():
        with open(test_findings_path, "r", encoding="utf-8") as f:
            findings = json.load(f)
        sliced = slice_all(findings, test_contract)
        print(f"Sliced {len(sliced)} findings against {test_contract.name}")
    else:
        print("Missing test contract or test findings file for standalone run.")