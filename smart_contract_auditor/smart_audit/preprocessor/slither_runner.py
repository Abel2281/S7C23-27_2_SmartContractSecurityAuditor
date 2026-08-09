import json
import subprocess
from pathlib import Path
from typing import Dict, Any, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONTRACTS_DIR = PROJECT_ROOT / "contracts"
OUTPUT_DIR = PROJECT_ROOT / "output"


def resolve_target_path(target_contract_path: str) -> Optional[Path]:
    """Resolve .sol path. Accept bare filename (looked up in contracts/) or relative/absolute path."""
    p = Path(target_contract_path)

    candidates = [
        p if p.is_absolute() else None,
        PROJECT_ROOT / p,
        CONTRACTS_DIR / p.name,
    ]

    for c in candidates:
        if c and c.exists():
            return c.resolve()

    print(f"[Error] File not found. Tried: {[str(c) for c in candidates if c]}")
    return None


def resolve_output_path(output_json_path: str) -> Path:
    """Always write output under OUTPUT_DIR, regardless of cwd."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    p = Path(output_json_path)
    return (OUTPUT_DIR / p.name).resolve()


def run_slither(target_contract_path: str, output_json_path: str = "output_raw.json") -> Optional[Dict[str, Any]]:
    abs_target_path = resolve_target_path(target_contract_path)
    if not abs_target_path:
        return None

    abs_output_path = resolve_output_path(output_json_path)

    cmd = [
        "slither",
        str(abs_target_path),
        "--compile-force-framework", "solc",
        "--json", str(abs_output_path),
    ]

    print(f"[slither_runner] Executing: {' '.join(cmd)}")

    try:
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        if abs_output_path.exists() and abs_output_path.stat().st_size > 0:
            with open(abs_output_path, "r", encoding="utf-8") as f:
                raw_data = json.load(f)
            print(f"[slither_runner] Success! Raw output saved to: {abs_output_path}")
            return raw_data

        print(f"[Error] Slither completed but output file missing/empty.\nExit code: {result.returncode}\nStderr: {result.stderr}")
        return None

    except FileNotFoundError:
        print("[Error] 'slither' not found on PATH. Install it or activate venv.")
        return None
    except json.JSONDecodeError as e:
        print(f"[Error] Output file not valid JSON: {e}")
        return None
    except Exception as e:
        print(f"[Error] Failed to execute Slither: {e}")
        return None


if __name__ == "__main__":
    test_contract = "0x01f8c4e3fa3edeb29e514cba738d87ce8c091d3f.sol"
    data = run_slither(test_contract, "output_raw.json")
    print(f"Detectors found: {len(data.get('results', {}).get('detectors', [])) if data else 0}")