"""Replay a frozen multi-turn workload through the PD completions endpoint."""

import argparse
import asyncio
import hashlib
import heapq
import json
import statistics
import time
from collections import defaultdict, deque
from pathlib import Path

import aiohttp


PERIOD = 0.25


async def send(client, url, row, body, events):
    start = time.monotonic()
    reply = {
        "id": row["id"],
        "session": row["session"],
        "turn": row["turn"],
        "input_tokens": row["prompt_tokens"],
        "send_monotonic": start,
        "send_wall_time": time.time(),
    }
    done = False
    usage = None
    try:
        headers = {"Content-Type": "application/json", "X-Request-Id": row["id"]}
        async with client.post(url, data=body, headers=headers) as response:
            reply["http_status"] = response.status
            if response.status != 200:
                raise RuntimeError(f"HTTP {response.status}: {(await response.text())[:1000]}")
            if "text/event-stream" not in response.headers.get("Content-Type", ""):
                raise RuntimeError(f"Expected event stream: {(await response.text())[:1000]}")
            async for line in response.content:
                if not line.startswith(b"data:"):
                    continue
                payload = line[5:].strip()
                if payload == b"[DONE]":
                    done = True
                    break
                event = json.loads(payload)
                if "error" in event:
                    raise RuntimeError(str(event["error"])[:1000])
                if event.get("usage") is not None:
                    usage = event["usage"]
                if event.get("id"):
                    reply["response_id"] = event["id"]
                for choice in event.get("choices") or ():
                    # A single generated token can decode to empty text.
                    if "ttft_s" not in reply and (choice.get("text") or choice.get("finish_reason") == "length"):
                        reply["ttft_s"] = time.monotonic() - start
                        reply["first_text"] = choice.get("text", "")
                if usage is not None:
                    reply["server_input_tokens"] = usage["prompt_tokens"]
                    reply["output_tokens"] = usage["completion_tokens"]
        if not done or "ttft_s" not in reply or usage is None:
            raise RuntimeError("Missing first token, usage, or completed stream")
        if reply["server_input_tokens"] != row["prompt_tokens"] or reply["output_tokens"] != 1:
            raise RuntimeError(f"Unexpected token counts: {usage}")
        reply["status"] = "ok"
    except Exception as exc:
        reply["status"] = "error"
        reply["error"] = f"{type(exc).__name__}: {exc}"
    reply["completed_monotonic"] = time.monotonic()
    events.put_nowait(reply)


def prepare(workload, fixed_order):
    groups = defaultdict(list)
    bodies = {}
    seen = set()
    for line in workload.open():
        row = json.loads(line)
        if row["id"] in seen:
            raise ValueError(f"Repeated request ID {row['id']}")
        seen.add(row["id"])
        prompt = row.pop("prompt")
        if len(prompt) != row["prompt_tokens"]:
            raise ValueError(f"Unexpected prompt length for {row['id']}")
        request = {
            "model": "dsv4",
            "prompt": prompt,
            "max_tokens": 1,
            "temperature": 0,
            "ignore_eos": True,
            "add_special_tokens": False,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        bodies[row["id"]] = json.dumps(request, separators=(",", ":")).encode()
        groups[row["session"]].append(row)
    if fixed_order:
        priorities = sorted(row["priority"] for rows in groups.values() for row in rows)
        if priorities != list(range(len(bodies))):
            raise ValueError("Fixed priorities must be contiguous, unique slots starting at 0")
    pending = {}
    ready = []
    for session, records in groups.items():
        records.sort(key=lambda row: row["turn"])
        if [row["turn"] for row in records] != list(range(len(records))):
            raise ValueError(f"Broken session continuity: {session}")
        if fixed_order and any(left["priority"] >= right["priority"] for left, right in zip(records, records[1:])):
            raise ValueError(f"Fixed slots violate turn order for session {session}")
        pending[session] = deque(records)
        push_next(pending[session], ready, fixed_order)
    if not ready:
        raise ValueError("Empty workload")
    return pending, bodies, ready


def push_next(session, ready, fixed_order):
    if session:
        row = session.popleft()
        # Leave terminal turns available late in the trace, when earlier turns
        # may still be running and unable to release their session successors.
        queue_rank = 0 if fixed_order else -len(session)
        heapq.heappush(ready, (queue_rank, row["priority"], row["id"], row))


async def replay(args):
    fixed_order = args.order == "fixed"
    pending, bodies, ready = prepare(args.workload, fixed_order)
    url = args.proxy.rstrip("/") + "/v1/completions"
    events = asyncio.Queue()
    active = {}
    results = []
    send_order = []
    skipped = 0
    args.out.mkdir(parents=True, exist_ok=False)
    with (args.out / "requests.jsonl").open("x", buffering=1) as record:
        async with aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(limit=0),
            timeout=aiohttp.ClientTimeout(total=args.timeout),
            trust_env=False,
        ) as client:
            tick = time.monotonic()
            while ready or active:
                if ready or any(pending.values()):
                    await asyncio.sleep(max(0, tick - time.monotonic()))
                elif events.empty():
                    reply = await events.get()
                    events.put_nowait(reply)
                while not events.empty():
                    reply = events.get_nowait()
                    await active.pop(reply["id"])
                    results.append(reply)
                    record.write(json.dumps(reply, ensure_ascii=False) + "\n")
                    if reply["status"] == "ok":
                        push_next(pending[reply["session"]], ready, fixed_order)
                    elif fixed_order:
                        ready.clear()
                        for session in pending.values():
                            session.clear()
                    else:
                        pending[reply["session"]].clear()
                if ready and (not fixed_order or ready[0][1] == len(send_order)):
                    _, _, request_id, row = heapq.heappop(ready)
                    send_order.append(request_id)
                    active[request_id] = asyncio.create_task(send(client, url, row, bodies[request_id], events))
                elif ready or any(pending.values()):
                    skipped += 1
                tick += PERIOD
                if tick <= time.monotonic():
                    tick = time.monotonic() + PERIOD
    summarize(args, bodies, results, send_order, skipped)


def summarize(args, bodies, results, send_order, skipped):
    starts = sorted(row["send_monotonic"] for row in results)
    successful = [row for row in results if row["status"] == "ok"]
    ttft = sorted(row["ttft_s"] for row in successful)
    span = starts[-1] - starts[0] if len(starts) > 1 else 0
    elapsed = max(row["completed_monotonic"] for row in results) - starts[0]
    summary = {
        "workload_sha256": hashlib.sha256(args.workload.read_bytes()).hexdigest(),
        "scheduling_policy": (
            "fixed-slot-order, dependency-checked" if args.order == "fixed"
            else "longest-ready-chain-first, frozen-priority-tie-break"
        ),
        "request_rate": 1 / PERIOD,
        "offered_rps": (len(starts) - 1) / span if span else None,
        "offered_input_tokens_per_second": sum(row["input_tokens"] for row in results) / (span + PERIOD),
        "request_throughput": len(successful) / elapsed,
        "duration_s": elapsed,
        "send_span_s": span,
        "inflight_at_last_send": sum(row["completed_monotonic"] > starts[-1] for row in results),
        "missed_slots_for_dependency": skipped,
        "requested": len(bodies),
        "sent": len(results),
        "completed": len(successful),
        "failed": len(results) - len(successful),
        "unsent": sorted(set(bodies) - set(send_order)),
        "mean_ttft_ms": 1000 * statistics.fmean(ttft) if ttft else None,
        "median_ttft_ms": 1000 * statistics.median(ttft) if ttft else None,
        "p95_ttft_ms": 1000 * statistics.quantiles(ttft, n=100, method="inclusive")[94] if len(ttft) >= 2 else None,
    }
    (args.out / "send-order.json").write_text(json.dumps(send_order) + "\n")
    serialized = json.dumps(summary, indent=2) + "\n"
    (args.out / "summary.json").write_text(serialized)
    if len(successful) != len(bodies):
        unsent = summary.pop("unsent")
        print(json.dumps({**summary, "unsent_count": len(unsent)}, indent=2))
        raise SystemExit("Incomplete replay; inspect per-request evidence")
    print(serialized, end="")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proxy", required=True)
    parser.add_argument("--workload", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--order", choices=("ready", "fixed"), default="ready")
    asyncio.run(replay(parser.parse_args()))
