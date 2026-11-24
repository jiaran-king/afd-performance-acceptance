"""Freeze consecutive real turns from the archived V4 WildChat workload."""

import argparse
import hashlib
import json
import random
import statistics
from pathlib import Path


ARCHIVE_FILES = {
    "source_manifest.json": "598e5a103d04a94b5de47647ca1c8cb4f39b9ad25b733b6ed52aa1a60e36d63d",
    "selection/mapping.jsonl": "cce77faf0841c5572b9bf796e183a9cff3f365d5ad61bbb7c3de2d7aa31aaa75",
    "workloads/formal_0_requests.jsonl": "b9a1c31e74345273010a08d238cb18ecca6f941a9afe6d7e7a14f730266c3142",
    "workloads/formal_1_requests.jsonl": "47eeb562ae64438888a1b5c91cf30c02eee6a0edeb9cde14e95a08a86ba5943d",
    "workloads/formal_2_requests.jsonl": "c0ddfbb458552786672456f789bd69f79f8911cfeaed5278ba0d239a96a71656",
}
TOKENIZER_SHA256 = "8f9f37ca37fdc4f5fd36d5cf4d3b0e8392edb4e894fd10cc0d70b4957c8633cf"
BOS, EOS, USER, ASSISTANT, THINK_END = 0, 1, 128803, 128804, 128822
INPUT_CAP = 32768
SEED = 20261123


def digest(payload):
    return hashlib.sha256(payload).hexdigest()


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


def earlier_prompt(prompt, source_turn):
    users = [index for index, token in enumerate(prompt) if token == USER]
    assistants = [index for index, token in enumerate(prompt) if token == ASSISTANT]
    if (
        len(users) != source_turn + 1
        or len(assistants) != len(users)
        or prompt[0] != BOS
        or users[0] != 1
        or assistants[-1] != len(prompt) - 2
        or not all(user < assistant for user, assistant in zip(users, assistants))
        or not all(prompt[position + 1] == THINK_END for position in assistants)
        or not all(assistant < user - 1 and prompt[user - 1] == EOS
                   for assistant, user in zip(assistants, users[1:]))
    ):
        raise ValueError("Archived prompt does not match its recorded V4 turn boundaries")
    return prompt[:assistants[-2] + 2]


def read_archive(bundle, min_input_tokens):
    for name, expected in ARCHIVE_FILES.items():
        if digest((bundle / name).read_bytes()) != expected:
            raise ValueError(f"Archive checksum mismatch for {name}")
    with (bundle / "selection/mapping.jsonl").open() as source:
        mapping = {row["request_id"]: row for row in map(json.loads, source)}
    pairs = []
    conversations = set()
    for ordinal in range(3):
        with (bundle / f"workloads/formal_{ordinal}_requests.jsonl").open() as source:
            for row in map(json.loads, source):
                source_id = row["request_id"]
                meta = mapping[source_id]
                source_turn = meta["user_turn_ordinal"]
                if source_turn < 1 or row["input_length"] > INPUT_CAP:
                    continue
                prompt = row["prompt_token_ids"]
                conversation = meta["conversation_id"]
                if (
                    conversation in conversations
                    or meta["message_count"] != 2 * source_turn + 1
                    or row["candidate_id"] != meta["candidate_id"]
                    or len(prompt) != row["input_length"]
                    or len(prompt) != meta["actual_input_length"]
                    or digest(encode(prompt)) != row["prompt_token_ids_sha256"]
                ):
                    raise ValueError(f"Unverified source payload for {source_id}")
                conversations.add(conversation)
                earlier = earlier_prompt(prompt, source_turn)
                if len(earlier) < min_input_tokens:
                    continue
                pairs.append([
                    {
                        "id": f"wildchat-{source_id}-turn-{original_turn}",
                        "session": conversation,
                        "turn": local_turn,
                        "source_user_turn": original_turn,
                        "source_shard": meta["source_shard"],
                        "source_row": meta["source_row_index"],
                        "archive_request_id": source_id,
                        "prompt": ids,
                        "prompt_tokens": len(ids),
                        "prompt_sha256": digest(encode(ids)),
                    }
                    for local_turn, (original_turn, ids) in enumerate(
                        ((source_turn - 1, earlier), (source_turn, prompt))
                    )
                ])
    return pairs


def fixed_slots(cohorts):
    rows = [pair[0] for pair in cohorts[0]]
    for previous, current in zip(cohorts, cohorts[1:]):
        for pending, new in zip(previous, current):
            rows.extend((pending[1], new[0]))
    rows.extend(pair[1] for pair in cohorts[-1])
    for slot, row in enumerate(rows):
        row["priority"] = slot
    return rows


def emit(root, name, cohorts):
    rows = fixed_slots(cohorts)
    target = root / name
    target.mkdir()
    payload = hashlib.sha256()
    with (target / "requests.jsonl").open("xb") as output:
        for row in rows:
            line = encode(row) + b"\n"
            output.write(line)
            payload.update(line)
    lengths = [row["prompt_tokens"] for row in rows]
    sent_at = {row["session"]: row["priority"] for row in rows if row["turn"] == 0}
    gaps = [row["priority"] - sent_at[row["session"]] for row in rows if row["turn"] == 1]
    manifest = {
        "sessions": len(sent_at),
        "requests": len(rows),
        "requests_sha256": payload.hexdigest(),
        "min_input_tokens": min(lengths),
        "mean_input_tokens": statistics.fmean(lengths),
        "median_input_tokens": statistics.median(lengths),
        "p95_input_tokens": statistics.quantiles(lengths, n=100, method="inclusive")[94],
        "max_input_tokens": max(lengths),
        "target_rps": 4,
        "offered_input_tokens_per_second": 4 * statistics.fmean(lengths),
        "scheduled_turn_gap_sec": [min(gaps) / 4, max(gaps) / 4],
        "scheduled_input_tokens_per_second_by_25s_window": [
            4 * statistics.fmean(lengths[index:index + 100])
            for index in range(0, len(rows), 100)
        ],
    }
    (target / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--min-input", type=int, default=0)
    args = parser.parse_args()
    pairs = read_archive(args.bundle, args.min_input)
    if len(pairs) < 808:
        raise ValueError(f"Need 808 distinct conversation pairs, found {len(pairs)}")
    rng = random.Random(SEED)
    rng.shuffle(pairs)
    sample = sorted(pairs[:800], key=lambda pair: sum(row["prompt_tokens"] for row in pair))
    cohorts = [sample[index::16] for index in range(16)]
    for cohort in cohorts:
        rng.shuffle(cohort)
    args.output.mkdir(parents=True, exist_ok=False)
    metadata = {
        "archive": "moonconv-wildchat-v4-flash-prefill bundle 8ab206ac9f01",
        "archive_files_sha256": ARCHIVE_FILES,
        "tokenizer_sha256": TOKENIZER_SHA256,
        "source_dataset": "allenai/WildChat-4.8M",
        "source_revision": "c827c6df8fcf008219ffaffa4d1dd77491099367",
        "source_sessions_under_cap": len(pairs),
        "min_input_tokens": args.min_input,
        "input_cap": INPUT_CAP,
        "selection_seed": SEED,
        "selection": "Sample unique archive conversations before any timings and allocate length-stratified 50-session cohorts",
        "turns": "Last two successive user turns in each archived V4 conversation prefix. The earlier prompt ends with its original V4 generation suffix. Later history uses the original response.",
        "schedule": "250 ms slots in a fixed order. After the initial first-turn cohort, interleave its successors with first turns from the next cohort. Later turns require the earlier request to finish.",
        "splits": {
            "qualification": emit(args.output, "qualification", cohorts[:4]),
            "formal_candidate": emit(args.output, "formal-candidate", cohorts[4:]),
            "warmup": emit(args.output, "warmup", [pairs[800:808]]),
        },
    }
    report = json.dumps(metadata, indent=2) + "\n"
    (args.output / "manifest.json").write_text(report)
    print(report, end="")


if __name__ == "__main__":
    main()
