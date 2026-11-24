"""Audit complete PD replay evidence and compare two runs of a frozen load."""

import argparse
import gzip
import hashlib
import json
import math
import re
import statistics
import time
from collections import defaultdict
from pathlib import Path


def external_kv(path):
    total = 0.0
    samples = 0
    for line in path.read_text().splitlines():
        if line.startswith("vllm:prompt_tokens_by_source_total{") and 'source="external_kv_transfer"' in line:
            total += float(line.split()[-1])
            samples += 1
    if not samples:
        raise ValueError(f"Missing external KV counter: {path}")
    return total


def queues(run):
    peaks = defaultdict(int)
    pattern = re.compile(r"(\d\d:\d\d:\d\d).*Engine\s+(\d+):.*Waiting:\s*(\d+)\s+req", re.I)
    totals = defaultdict(dict)
    for log in sorted(run.glob("*-window.log")):
        if log.name not in {"p0-window.log", "p1-window.log", "prefill_attention_node0-window.log"}:
            continue
        for when, rank, waiting in pattern.findall(log.read_text()):
            engine = f"{log.stem}:{rank}"
            peaks[engine] = max(peaks[engine], int(waiting))
            totals[when][engine] = int(waiting)
    state = {}
    series = []
    for when, samples in sorted(totals.items()):
        state.update(samples)
        series.append({"time": when, "waiting": sum(state.values())})
    return dict(peaks), series


def prefill_phases(run):
    metrics = re.compile(
        r'^vllm:request_(queue|prefill)_time_seconds_(sum|count)\{[^\n]+\}\s+([\d.eE+-]+)$', re.M
    )

    def totals(paths):
        result = defaultdict(float)
        for path in paths:
            for phase, aggregate, value in metrics.findall(path.read_text()):
                result[phase, aggregate] += float(value)
        return result

    before = totals(run.glob("pre-71*.metrics"))
    after = totals(run.glob("post-71*.metrics"))
    result = {}
    for phase in ("queue", "prefill"):
        count = after[phase, "count"] - before[phase, "count"]
        seconds = after[phase, "sum"] - before[phase, "sum"]
        if count > 0 and seconds >= 0:
            result[phase] = {"requests": int(count), "mean_ms": seconds / count * 1000}
    return result


def arrival_windows(order):
    windows = []
    for first in range(0, len(order), 100):
        block = order[first:first + 100]
        start = block[0]["send_monotonic"]
        after = first + len(block)
        end = order[after]["send_monotonic"] if after < len(order) else block[-1]["send_monotonic"] + 0.25
        completed = [row for row in order if start <= row["completed_monotonic"] < end]
        windows.append({
            "first_slot": first,
            "seconds": end - start,
            "sent": len(block),
            "completed": len(completed),
            "input_tokens_sent_per_second": sum(row["input_tokens"] for row in block) / (end - start),
            "input_tokens_completed_per_second": sum(row["input_tokens"] for row in completed) / (end - start),
            "inflight_at_end": sum(row["completed_monotonic"] >= end for row in order[:after]),
            "mean_ttft_ms": 1000 * statistics.fmean(row["ttft_s"] for row in block),
        })
    return windows


def read_rows(path):
    if path.exists():
        with path.open(encoding="utf-8") as stream:
            return list(map(json.loads, stream))
    with gzip.open(str(path) + ".gz", "rt", encoding="utf-8") as stream:
        return list(map(json.loads, stream))


def response_ids(requests_file):
    return {
        row["response_id"] for row in read_rows(requests_file)
        if row["status"] == "ok"
    }


def transfer_ids(log):
    return set(re.findall(r"KV cache transfer for request (\S+) took", log))


def transfer_owner(transfer):
    return re.sub(r"-[0-9]+-[0-9a-f]{8}$", "", transfer)


def snapshot_decode_log(source, offset, run, wait_for_transfers):
    expected = response_ids(run / "client/requests.jsonl") if wait_for_transfers else set()
    deadline = time.monotonic() + 30
    while True:
        with source.open("rb") as stream:
            stream.seek(offset)
            raw = stream.read()
        owners = {transfer_owner(identifier) for identifier in transfer_ids(raw.decode(errors="replace"))}
        if expected <= owners or time.monotonic() >= deadline:
            (run / "decode-window.log").write_bytes(raw)
            return
        # D writes from another node. The HTTP response may arrive before NFS exposes the log.
        time.sleep(1)


def independently_audit(run, workload, digest, warmup):
    client = run / "client"
    meta = json.loads((client / "summary.json").read_text())
    rows = read_rows(client / "requests.jsonl")
    if len(rows) != len(workload) or set(row["id"] for row in rows) != set(workload):
        raise ValueError(f"Missing or repeated workload requests in {run}")
    if meta["workload_sha256"] != digest or meta["request_rate"] != 4:
        raise ValueError(f"Replay used a different workload or target rate: {run}")
    ends = {}
    order = sorted(rows, key=lambda row: row["send_monotonic"])
    for row in order:
        original = workload[row["id"]]
        send, done, elapsed = row["send_monotonic"], row["completed_monotonic"], row["ttft_s"]
        if (
            row["status"] != "ok"
            or row["http_status"] != 200
            or original != (row["session"], row["turn"], row["server_input_tokens"])
            or row["output_tokens"] != 1
            or not (
                all(math.isfinite(value) for value in (send, done, elapsed))
                and 0 < elapsed <= done - send
            )
        ):
            raise ValueError(f"Invalid request trace in {run}: {row['id']}")
        session = row["session"]
        turn = row["turn"]
        if turn and (session, turn - 1) not in ends:
            raise ValueError(f"Missing predecessor for {row['id']}")
        if turn and send < ends[session, turn - 1]:
            raise ValueError(f"Concurrent turns within session {session} in {run}")
        ends[session, turn] = done
    times = [row["send_monotonic"] for row in order]
    ttft = [row["ttft_s"] * 1000 for row in order]
    keys = list(workload)
    sent_ids = [row["id"] for row in order]
    if meta.get("scheduling_policy") == "fixed-slot-order, dependency-checked" and sent_ids != keys:
        raise ValueError(f"Observed sending order disagrees with the frozen slots in {run}")
    arrivals = (len(times) - 1) / (times[-1] - times[0])
    decode = (run / "decode-window.log").read_text()
    kv_transfers = transfer_ids(decode)
    responses = {row["response_id"] for row in rows}
    matched = []
    known_warmup = response_ids(warmup / "requests.jsonl") if warmup else set()
    warmup_transfers = 0
    unexpected = []
    for transfer in kv_transfers:
        owner = transfer_owner(transfer)
        if owner in responses:
            matched.append(owner)
        elif owner in known_warmup:
            warmup_transfers += 1
        else:
            unexpected.append(transfer)
    transfer_errors = [
        line
        for line in decode.splitlines()
        if "ERROR" in line and any(word in line.lower() for word in ("transfer", "mooncake"))
    ]
    matched_tokens = round(external_kv(run / "post-d.metrics") - external_kv(run / "pre-d.metrics"))
    expected_tokens = sum(prompt_len - 1 for _, _, prompt_len in workload.values())
    if (transfer_errors or unexpected or len(matched) != len(keys)
            or set(matched) != responses or matched_tokens != expected_tokens):
        raise ValueError(
            f"D-side KV audit failed in {run}: {len(set(matched))}/{len(keys)} matched requests, "
            f"{len(unexpected)} unrelated transfers, {matched_tokens}/{expected_tokens} external tokens, "
            f"{len(transfer_errors)} errors"
        )
    peaks, waiting = queues(run)
    return {
        "run": str(run.resolve()),
        "workload_sha256": digest,
        "scheduling_policy": meta.get("scheduling_policy", "round-first"),
        "completed": len(order),
        "observed_rps": arrivals,
        "missed_slots_for_dependency": meta["missed_slots_for_dependency"],
        "input_tokens_per_second": sum(row["input_tokens"] for row in order) / (times[-1] - times[0] + 0.25),
        "request_throughput": len(order) / (max(row["completed_monotonic"] for row in order) - times[0]),
        "mean_ttft_ms": statistics.fmean(ttft),
        "median_ttft_ms": statistics.median(ttft),
        "duration_s": max(row["completed_monotonic"] for row in order) - times[0],
        "p95_ttft_ms": statistics.quantiles(ttft, n=100, method="inclusive")[94],
        "ttft_quarter_means_ms": [statistics.fmean(ttft[i * len(ttft) // 4 : (i + 1) * len(ttft) // 4]) for i in range(4)],
        "inflight_at_last_send": sum(row["completed_monotonic"] > times[-1] for row in order),
        "decoder_kv_transfers": len(matched),
        "decoder_external_kv_tokens": matched_tokens,
        "decoder_warmup_transfers_in_log_window": warmup_transfers,
        "p_request_phase_metrics": prefill_phases(run),
        "p_peak_waiting_by_engine": peaks,
        "p_waiting_timeseries": waiting,
        "arrival_windows": arrival_windows(order),
    }, sent_ids


def load_workload_index(rows):
    keys = {}
    for entry in rows:
        if entry["id"] in keys:
            raise ValueError(f"Duplicate workload ID {entry['id']}")
        keys[entry["id"]] = (entry["session"], entry["turn"], entry["prompt_tokens"])
    return keys


def at_target_rate(run):
    return (
        abs(run["observed_rps"] - 4) <= 0.01
        and run["missed_slots_for_dependency"] == 0
        and run["scheduling_policy"] == "fixed-slot-order, dependency-checked"
    )


def compare(keys, digest, baseline_run, afd_run=None, baseline_warmup=None, afd_warmup=None):
    baseline, order = independently_audit(baseline_run, keys, digest, baseline_warmup)
    result = {"baseline": baseline, "replay_valid": at_target_rate(baseline)}
    if afd_run is not None:
        afd, other_order = independently_audit(afd_run, keys, digest, afd_warmup)
        if baseline["scheduling_policy"] != afd["scheduling_policy"]:
            raise ValueError("The two runs scheduled ready requests under different rules")
        ceiling = baseline["mean_ttft_ms"] * 0.8
        valid = result["replay_valid"] and at_target_rate(afd) and order == other_order
        result.update(
            afd=afd,
            identical_send_order=order == other_order,
            acceptance_ceiling_ms=ceiling,
            reduction_pct=(1 - afd["mean_ttft_ms"] / baseline["mean_ttft_ms"]) * 100,
            ttft_gate_passed=afd["mean_ttft_ms"] <= ceiling,
            replay_valid=valid,
            acceptance_passed=valid and afd["mean_ttft_ms"] <= ceiling,
        )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workload", type=Path)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("afd", type=Path, nargs="?")
    parser.add_argument("--baseline-warmup", type=Path)
    parser.add_argument("--afd-warmup", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--check", action="store_true", help="Exit nonzero if the replay or TTFT gate fails")
    args = parser.parse_args()
    digest = hashlib.sha256(args.workload.read_bytes()).hexdigest()
    keys = load_workload_index(read_rows(args.workload))
    result = compare(keys, digest, args.baseline, args.afd, args.baseline_warmup, args.afd_warmup)
    raw = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(raw)
    print(raw, end="")
    if args.check and not result.get("acceptance_passed", result["replay_valid"]):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
