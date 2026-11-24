"""Materialize the exact AFD launch-script adjustments used by the case."""

import shutil
import sys
from pathlib import Path


def replace_once(text, anchor, replacement):
    if text.count(anchor) != 1:
        raise ValueError(f"Expected a single upstream anchor: {anchor!r}")
    return text.replace(anchor, replacement, 1)


def adapt(source, destination):
    shutil.copytree(source, destination)
    rank = destination / "run_prefill_full_rank.sh"
    text = rank.read_text()
    text = replace_once(
        text,
        '  "enable_cpu_binding": %s,',
        '  "enable_dsv4_shared_compressor_workspace": true,\n'
        '  "enable_prefill_mc2": true,\n  "enable_cpu_binding": %s,',
    )
    text = replace_once(text, "--enable-chunked-prefill", "--enable-chunked-prefill --async-scheduling")
    text = replace_once(
        text, r'\"engine_id\": \"0\",', r'\"engine_id\": \"${PD_KV_PREFILL_ENGINE_ID:-0}\",'
    )
    rank.write_text(text)
    attention = destination / "run_prefill_attention.sh"
    attention.write_text(replace_once(
        attention.read_text(), r'  \"enable_cpu_binding\": ${ENABLE_CPU_BINDING},',
        r'  \"enable_dsv4_shared_compressor_workspace\": true,' + "\n"
        + r'  \"enable_cpu_binding\": ${ENABLE_CPU_BINDING},',
    ))
    for name in ("run_prefill_full_rank", "run_prefill_attention", "run_prefill_ffn", "run_decode"):
        if "--no-enable-prefix-caching" not in (destination / f"{name}.sh").read_text():
            raise ValueError(f"{name} does not disable prefix caching")


if __name__ == "__main__":
    adapt(Path(sys.argv[1]), Path(sys.argv[2]))
