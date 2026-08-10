"""
Strips Slither raw JSON noise, keeps only actionable findings, sorts by
severity+confidence tier, optional cap via max_findings.
"""

import hashlib
import json
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

VALID_IMPACTS = {"High", "Medium"}

TIER_MAP = {
    ("High", "High"): 1,
    ("High", "Medium"): 2,
    ("Medium", "High"): 2,
    ("Medium", "Medium"): 3,
}


def _extract_locations(elements: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Pull filename + exact line range + function/contract name from EVERY
    element with a source_mapping. One entry per element, nothing merged."""

    locations = []

    for el in elements:
        mapping = el.get("source_mapping", {})
        lines = mapping.get("lines", [])
        if not lines:
            continue

        filename = (
            mapping.get("filename_absolute")
            or mapping.get("filename_relative")
            or mapping.get("filename_short")
        )

        el_type = el.get("type")
        name = el.get("name")
        function_name = name if el_type == "function" else None
        contract_name = name if el_type == "contract" else None

        parent = el.get("type_specific_fields", {}).get("parent", {})
        if parent.get("type") == "contract" and contract_name is None:
            contract_name = parent.get("name")
        elif parent.get("type") == "function":
            if function_name is None:
                function_name = parent.get("name")
            grandparent = parent.get("type_specific_fields", {}).get("parent", {})
            if grandparent.get("type") == "contract" and contract_name is None:
                contract_name = grandparent.get("name")

        locations.append({
            "filename": filename,
            "start_line": min(lines),
            "end_line": max(lines),
            "function_name": function_name,
            "contract_name": contract_name,
        })

    return locations


def _aggregate_locations(locations: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Group spans by (contract, function). NO min/max collapsing — every
    distinct span Slither reported is kept as its own entry under that
    function, so scattered/disjoint lines (e.g. line 10-12 AND line 300-305
    in the same function) stay separate instead of merging into one huge
    range."""
    if not locations:
        return None

    primary_filename = locations[0]["filename"]
    same_file = [l for l in locations if l["filename"] == primary_filename]

    func_groups: Dict[Tuple[Optional[str], str], List[Dict[str, int]]] = {}
    unassigned: List[Dict[str, int]] = []

    for l in same_file:
        span = {"start_line": l["start_line"], "end_line": l["end_line"]}
        if l["function_name"]:
            key = (l["contract_name"], l["function_name"])
            func_groups.setdefault(key, []).append(span)
        else:
            unassigned.append(span)

    related_functions = []
    for (_contract, name), spans in func_groups.items():
        # dedupe identical spans, keep every distinct one, sorted by start
        uniq_spans = sorted({(s["start_line"], s["end_line"]) for s in spans})
        related_functions.append({
            "name": name,
            "lines": [{"start_line": s, "end_line": e} for s, e in uniq_spans],
        })

    related_functions.sort(key=lambda f: f["lines"][0]["start_line"])

    contract_name = next((l["contract_name"] for l in locations if l["contract_name"]), None)

    return {
        "filename": primary_filename,
        "contract_name": contract_name,
        "related_functions": related_functions,
        "unassigned_lines": unassigned,  # spans with no function context (state vars, pragma, etc.)
    }


def _make_finding_id(check: str, agg: Dict[str, Any]) -> str:
    """Stable id: same check + same exact spans => same id across runs."""
    parts = []
    for func in agg["related_functions"]:
        span_str = ";".join(f"{s['start_line']}-{s['end_line']}" for s in func["lines"])
        parts.append(f"{func['name']}:{span_str}")
    funcs = ",".join(parts)
    raw = f"{check}|{agg['filename']}|{agg['contract_name']}|{funcs}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def filter_findings(
    raw_data: Dict[str, Any],
    max_findings: Optional[int] = None,
) -> List[Dict[str, Any]]:

    detectors = raw_data.get("results", {}).get("detectors", [])
    filtered = []

    for det in detectors:
        impact = det.get("impact")
        if impact not in VALID_IMPACTS:
            continue  # fast reject before touching nested elements

        confidence = det.get("confidence")
        tier = TIER_MAP.get((impact, confidence))
        if tier is None:
            continue

        elements = det.get("elements", [])
        locations = _extract_locations(elements)
        agg = _aggregate_locations(locations)
        if agg is None:
            continue  # no usable source location, code_slicer can't act on it

        check = det.get("check")

        filtered.append({
            "finding_id": _make_finding_id(check, agg),
            "check": check,
            "impact": impact,
            "confidence": confidence,
            "description": det.get("description", "").strip(),
            "contract_name": agg["contract_name"],
            "filename": agg["filename"],
            "related_functions": agg["related_functions"],
            "unassigned_lines": agg["unassigned_lines"],
            "_tier": tier,
        })

    filtered.sort(key=lambda f: f["_tier"])

    if max_findings is not None:
        filtered = filtered[:max_findings]

    for f in filtered:
        del f["_tier"]

    return filtered


def save_findings(findings: List[Dict[str, Any]], output_json_path: str = "filtered_findings.json") -> Path:
    project_root = Path(__file__).resolve().parents[2]
    output_dir = project_root / "output"
    output_dir.mkdir(parents=True, exist_ok=True)

    out_path = output_dir / Path(output_json_path).name
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(findings, f, indent=2)

    return out_path


if __name__ == "__main__":
    PROJECT_ROOT = Path(__file__).resolve().parents[2]
    test_path = PROJECT_ROOT / "output" / "output_raw.json"

    if test_path.exists():
        with open(test_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        result = filter_findings(raw, max_findings=10)
        print(f"Findings after filter: {len(result)}")

        out_path = save_findings(result, "filtered_findings.json")
        print(f"[json_filter] Saved to: {out_path}")
    else:
        print(f"No test file at {test_path}, run slither_runner.py first.")