"""Exercise an explicitly selected P through a PD proxy and audit remote KV use."""

import argparse
import gzip
import hashlib
import json
import re
import time
from pathlib import Path
from urllib.request import ProxyHandler, Request, build_opener


def metrics(opener, decoder):
    with opener.open(f"http://{decoder}/metrics", timeout=15) as response:
        raw = response.read().decode()
    pattern = (
        r'^vllm:prompt_tokens_by_source_total\{[^\n]*'
        r'source="external_kv_transfer"[^\n]*\}\s+([0-9.eE+-]+)'
    )
    samples = re.findall(pattern, raw, flags=re.MULTILINE)
    if not samples:
        raise RuntimeError("Decoder did not export external KV token counters")
    return raw, sum(float(sample) for sample in samples)


def choose_prompt(dataset, min_prompt_tokens):
    if dataset is None:
        return (
            "A library keeps its books on shelves, and each shelf has a unique number. "
            "The catalog lists the shelf number and the title for every book. "
        ) * 80 + "\nIn one phrase, what does the catalog list?"
    open_data = gzip.open if dataset.suffix == ".gz" else open
    with open_data(dataset, "rt", encoding="utf-8") as workload:
        for line in workload:
            row = json.loads(line)
            if row["metadata"]["input_length"] >= min_prompt_tokens:
                return row["prompt"]
    raise ValueError(f"{dataset} has no prompt of at least {min_prompt_tokens} tokens")


def generate(opener, proxy, raw_path, prompt):
    data = {
        "model": "dsv4",
        "prompt": prompt,
        "max_tokens": 8,
        "temperature": 0,
        "ignore_eos": True,
        "stream": True,
        "stream_options": {"include_usage": True},
        "add_special_tokens": False,
    }
    req = Request(
        f"http://{proxy}/v1/completions",
        data=json.dumps(data).encode(),
        headers={"Content-Type": "application/json"},
    )
    first_token = None
    complete = False
    pieces = []
    usage = None
    start = time.monotonic()
    with raw_path.open("wb") as record, opener.open(req, timeout=180) as response:
        for line in response:
            record.write(line)
            if not line.startswith(b"data: "):
                continue
            payload = line[6:].strip()
            if payload == b"[DONE]":
                complete = True
                break
            event = json.loads(payload)
            if event.get("usage") is not None:
                usage = event["usage"]
            for choice in event.get("choices", []):
                value = choice.get("text")
                if value:
                    first_token = first_token if first_token is not None else time.monotonic() - start
                    pieces.append(value)
    if not complete or first_token is None or usage is None or usage["completion_tokens"] != 8:
        raise RuntimeError("Proxy stream did not complete with eight generated tokens")
    return first_token, "".join(pieces), usage["prompt_tokens"]


def decoder_transfers(log_path, offset):
    # The decoder writes from a different node. Give NFS time to expose its log tail.
    deadline = time.monotonic() + 45
    while True:
        with log_path.open("rb") as log:
            log.seek(offset)
            raw = log.read().decode(errors="replace")
        lines = [line for line in raw.splitlines() if "[mooncake_hybrid_connector.py:" in line]
        successful = sum("KV cache transfer for request " in line for line in lines)
        errors = sum(" ERROR " in line for line in lines)
        if successful or errors or time.monotonic() >= deadline:
            return raw, successful, errors
        time.sleep(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proxy", required=True)
    parser.add_argument("--decoder", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--decode-log", type=Path)
    parser.add_argument("--dataset", type=Path)
    parser.add_argument("--min-prompt-tokens", type=int, default=0)
    args = parser.parse_args()
    if args.min_prompt_tokens and args.dataset is None:
        parser.error("--min-prompt-tokens requires --dataset")
    prompt = choose_prompt(args.dataset, args.min_prompt_tokens)
    args.output.mkdir(parents=True, exist_ok=False)
    opener = build_opener(ProxyHandler({}))
    raw, before = metrics(opener, args.decoder)
    (args.output / "decode-metrics-before.txt").write_text(raw)
    if args.decode_log is not None:
        with args.decode_log.open("rb") as log:
            log_offset = log.seek(0, 2)
    ttft, generated, prompt_tokens = generate(opener, args.proxy, args.output / "response.sse", prompt)
    raw, after = metrics(opener, args.decoder)
    (args.output / "decode-metrics-after.txt").write_text(raw)
    result = {
        "proxy": args.proxy,
        "decoder": args.decoder,
        "ttft_seconds": ttft,
        "generated": generated,
        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "prompt_tokens": prompt_tokens,
        "decoder_external_kv_tokens": after - before,
    }
    if args.decode_log is not None:
        transfer_log, successful, errors = decoder_transfers(args.decode_log, log_offset)
        (args.output / "decode-kv-log.txt").write_text(transfer_log)
        result["decoder_successful_transfers"] = successful
        result["decoder_transfer_errors"] = errors
    report = json.dumps(result, ensure_ascii=False, indent=2)
    (args.output / "result.json").write_text(report + "\n")
    print(report)
    if after <= before:
        raise RuntimeError("The request did not increase the decoder external KV token count")
    if args.decode_log is not None and (
        not result["decoder_successful_transfers"] or result["decoder_transfer_errors"]
    ):
        raise RuntimeError("The decoder log did not confirm a successful and error-free KV transfer")
    if prompt_tokens < args.min_prompt_tokens:
        raise RuntimeError("Server token count was below the requested prompt floor")


if __name__ == "__main__":
    main()
