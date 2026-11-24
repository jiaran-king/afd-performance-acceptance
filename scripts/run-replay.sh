#!/usr/bin/env bash
set -euo pipefail

root=$(cd -- "$(dirname -- "$0")/.." && pwd)
source "$root/scripts/environment.sh"
name=${1:?Specify an unused run name}
p_run=${2:?Specify the live P run directory}
d_run=${3:?Specify the live D run directory}
workload=${4:?Specify frozen requests.jsonl}
mode=${5:?Specify baseline or afd}
order=${6:-ready}

case "$mode" in
    baseline) logs=(p0 p1); ports=(7100 7101) ;;
    afd) logs=(prefill_attention_node0 prefill_ffn_node0); ports=(7100) ;;
    *) echo "Unknown mode: $mode" >&2; exit 2 ;;
esac

out="${CASE_RUN_DIR:?Set CASE_RUN_DIR}/$name"
mkdir "$out"
trap 'printf "%s\n" "$?" > "$out/exit-code"' EXIT

for port in "${ports[@]}"; do
    curl -fsS --max-time 10 "http://$P_NODE_IP:$port/health"
    curl -fsS --max-time 15 "http://$P_NODE_IP:$port/metrics" > "$out/pre-$port.metrics"
done
curl -fsS --max-time 10 "http://$D_NODE_IP:7100/health"
curl -fsS --max-time 15 "http://$D_NODE_IP:7100/metrics" > "$out/pre-d.metrics"
d_offset=$(stat -c %s "$d_run/decode.log")
printf '%s\n' "$d_offset" > "$out/decode-start-byte-offset"
offsets=()
for role in "${logs[@]}"; do
    offsets+=("$(stat -c %s "$p_run/$role.log")")
done

set +e
python3 -B "$root/scripts/replay.py" --proxy "http://$P_NODE_IP:8000" \
    --workload "$workload" --out "$out/client" --order "$order"
status=$?
set -e

for port in "${ports[@]}"; do
    curl -fsS --max-time 15 "http://$P_NODE_IP:$port/metrics" > "$out/post-$port.metrics" || true
done
curl -fsS --max-time 15 "http://$D_NODE_IP:7100/metrics" > "$out/post-d.metrics" || true
PYTHONPATH="$root/scripts${PYTHONPATH:+:$PYTHONPATH}" python3 -B - "$d_run/decode.log" "$d_offset" "$out" "$status" <<'PY'
from pathlib import Path
import sys

from audit import snapshot_decode_log

source, offset, run, status = sys.argv[1:]
snapshot_decode_log(Path(source), int(offset), Path(run), status == "0")
PY
for i in "${!logs[@]}"; do
    role=${logs[i]}
    tail -c "+$((offsets[i] + 1))" "$p_run/$role.log" > "$out/$role-window.log"
done
exit "$status"
