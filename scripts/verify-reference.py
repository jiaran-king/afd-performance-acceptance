"""Recompute the published case from its prompt-free evidence."""

import hashlib
import json
from pathlib import Path

from audit import compare, load_workload_index, read_rows


ROOT = Path(__file__).resolve().parents[1]
CASE = ROOT / "cases/910c-dsv4-w4a8-rps4"
RESULT = ROOT / "results/2026-11-23"


def equivalent(expected, observed, path=()):
    if isinstance(expected, dict):
        for key, value in expected.items():
            if key != "run":
                equivalent(value, observed[key], (*path, key))
    elif expected != observed:
        raise ValueError(f"Reference mismatch at {'.'.join(path)}: {expected!r} != {observed!r}")


def main():
    plan = json.loads((CASE / "case.json").read_text())["load"]
    index = RESULT / "workload-index.jsonl"
    payload = Path(str(index) + ".gz").read_bytes()
    if hashlib.sha256(payload).hexdigest() != plan["reference_index_sha256"]:
        raise ValueError("Reference workload index checksum mismatch")
    formal = plan["splits"]["formal_candidate"]
    rows = load_workload_index(read_rows(index))
    if len(rows) != formal["requests"]:
        raise ValueError("Unexpected reference workload size")
    baseline, afd = (RESULT / "evidence" / arm for arm in ("baseline", "afd"))
    report = compare(rows, formal["sha256"], baseline, afd, baseline / "warmup", afd / "warmup")
    equivalent(json.loads((RESULT / "reference.json").read_text()), report)
    for arm in ("baseline", "afd"):
        report[arm]["run"] = f"results/2026-11-23/evidence/{arm}"
    print(json.dumps(report, indent=2))
    if not report["acceptance_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
