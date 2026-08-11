"""
code_slicer.py
Rule-based code slicer.

Input : output/filtered_findings.json   (from json_filter.py)
Reads : contracts/<basename>.sol        (matched by filename basename)
Output: output/sliced_findings.json

For each finding, for each related_function, merges that function's
distinct line spans (padded with CONTEXT_PADDING lines) into non-overlapping
blocks, then pulls the exact source text for each block. Per-function spans
are kept separate (not collapsed across functions) per existing span-handling
rule.
"""

import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONTRACTS_DIR = PROJECT_ROOT / "contracts"
FILTERED_FINDINGS_PATH = PROJECT_ROOT / "output" / "filtered_findings.json"
OUTPUT_PATH = PROJECT_ROOT / "output" / "sliced_findings.json"

CONTEXT_PADDING = 3  # lines of context before/after each span


def load_findings(path: Path = FILTERED_FINDINGS_PATH) -> list[dict]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_source_lines(contract_filename: str, cache: dict) -> list[str] | None:
    """
    Resolve source file by basename inside CONTRACTS_DIR (finding['filename']
    may hold an absolute path from a different machine). Cached per basename
    so each .sol file is read from disk only once.
    """
    basename = Path(contract_filename).name

    if basename in cache:
        return cache[basename]

    source_path = CONTRACTS_DIR / basename
    if not source_path.exists():
        cache[basename] = None
        return None

    with open(source_path, "r", encoding="utf-8", errors="replace") as f:
        lines = f.read().splitlines()

    cache[basename] = lines
    return lines


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


def slice_finding(finding: dict, source_cache: dict) -> dict:
    source_lines = load_source_lines(finding["filename"], source_cache)

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


def slice_all(findings: list[dict]) -> list[dict]:
    source_cache: dict = {}
    return [slice_finding(f, source_cache) for f in findings]


def save_slices(sliced: list[dict], path: Path = OUTPUT_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(sliced, f, indent=2)


def main():
    findings = load_findings()
    sliced = slice_all(findings)
    save_slices(sliced)
    print(f"Sliced {len(sliced)} findings -> {OUTPUT_PATH}")


if __name__ == "__main__":
    main()