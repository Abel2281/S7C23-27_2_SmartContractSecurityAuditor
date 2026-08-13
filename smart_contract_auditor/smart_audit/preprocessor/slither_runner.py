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


def _resolve_temp_output_path(temp_json_name: str) -> Path:
    """Slither itself forces a JSON write to disk (--json flag). We always
    write that forced file under OUTPUT_DIR and delete it right after
    parsing — it never becomes a pipeline artifact, just a scratch file."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    p = Path(temp_json_name)
    return (OUTPUT_DIR / p.name).resolve()


def run_slither(target_contract_path: Path, temp_json_name: str = "output_raw.json") -> Optional[Dict[str, Any]]:
    """
    Runs Slither on an already-resolved contract Path and returns the
    parsed raw JSON as an in-memory dict. Slither's own --json output is
    written to a scratch file under OUTPUT_DIR and deleted immediately
    after being read — it is not a pipeline-visible artifact.
    """
    abs_target_path = Path(target_contract_path).resolve()
    if not abs_target_path.exists():
        print(f"[Error] File not found: {abs_target_path}")
        return None

    abs_temp_path = _resolve_temp_output_path(temp_json_name)

    cmd = [
        "slither",
        str(abs_target_path),
        "--compile-force-framework", "solc",
        "--json", str(abs_temp_path),
    ]

    # print(f"[slither_runner] Executing: {' '.join(cmd)}")

    raw_data = None
    try:
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        if abs_temp_path.exists() and abs_temp_path.stat().st_size > 0:
            with open(abs_temp_path, "r", encoding="utf-8") as f:
                raw_data = json.load(f)
        else:
            print(f"[Error] Slither completed but output file missing/empty.\nExit code: {result.returncode}\nStderr: {result.stderr}")

    except FileNotFoundError:
        print("[Error] 'slither' not found on PATH. Install it or activate venv.")
    except json.JSONDecodeError as e:
        print(f"[Error] Output file not valid JSON: {e}")
    except Exception as e:
        print(f"[Error] Failed to execute Slither: {e}")
    finally:
        # scratch file only exists to satisfy slither's own --json requirement,
        # never a pipeline artifact -> always clean up
        abs_temp_path.unlink(missing_ok=True)

    return raw_data


if __name__ == "__main__":
    test_path = resolve_target_path("0x01f8c4e3fa3edeb29e514cba738d87ce8c091d3f.sol")
    if test_path:
        data = run_slither(test_path)
        print(f"Detectors found: {len(data.get('results', {}).get('detectors', [])) if data else 0}")