#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "$0")/environment.sh"

run=${CASE_RUN_DIR:?Set CASE_RUN_DIR}
group=${1:?Specify baseline, afd12 or afd8}
p_run="$run/$group-p"
d_run="$run/$group-d"
workload="$runtime/workload"
smoke_data="$runtime/afd-plugin/.agents/skills/deploy-dsv4-flash-async/assets/workloads/formal_0_1_2_vllm_bench.jsonl.gz"

healthy() {
    curl -fsS --max-time 3 "$1" >/dev/null 2>&1
}

ready() {
    healthy "http://$D_NODE_IP:7100/health" &&
    healthy "http://$P_NODE_IP:7100/health" || return 1
    case "$group" in
        baseline) healthy "http://$P_NODE_IP:7101/health" ;;
        afd12|afd8)
            local need count
            if [[ "$group" == afd12 ]]; then need=4; else need=8; fi
            count=$(grep -c 'workers run connector loop' "$p_run/prefill_ffn_node0.log" || true)
            [[ "$count" -ge "$need" ]]
            ;;
    esac
}

deadline=$((SECONDS + ${STARTUP_TIMEOUT_SECONDS:-1200}))
until ready; do
    if (( SECONDS >= deadline )); then
        echo "Startup timed out. See $p_run and $d_run." >&2
        exit 1
    fi
    sleep 5
done

probe() {
    local port=$1 label=$2
    python3 -B "$root/scripts/smoke.py" --proxy "$P_NODE_IP:$port" --decoder "$D_NODE_IP:7100" \
        --decode-log "$d_run/decode.log" --output "$p_run/$label-short"
    python3 -B "$root/scripts/smoke.py" --proxy "$P_NODE_IP:$port" --decoder "$D_NODE_IP:7100" \
        --decode-log "$d_run/decode.log" --output "$p_run/$label-long" --dataset "$smoke_data" --min-prompt-tokens 32000
}

if [[ "$group" == baseline ]]; then
    probe 8001 p0
    probe 8002 p1
else
    probe 8000 pd
fi
python3 -B "$root/scripts/runtime-maps.py" "$p_run" > "$p_run/native-maps.json"
python3 -B "$root/scripts/verify-native.py" "$p_run/native-maps.json" "$runtime" "$group"

if [[ "$group" == afd8 ]]; then
    exit 0
fi

warmup() {
    python3 -B "$root/scripts/replay.py" --proxy "http://$P_NODE_IP:8000" \
        --workload "$workload/warmup/requests.jsonl" --out "$run/$1"
}

measure() {
    bash "$root/scripts/run-replay.sh" "$1" "$p_run" "$d_run" "$workload/$2/requests.jsonl" "$3" fixed
}

if [[ "$group" == baseline ]]; then
    warmup qualification-warmup
    measure qualification qualification baseline
    python3 -B "$root/scripts/audit.py" "$workload/qualification/requests.jsonl" "$run/qualification" \
        --baseline-warmup "$run/qualification-warmup" --check > "$run/qualification/audit.json"
    python3 -B - "$run/qualification/audit.json" <<'PY'
import json, sys
a = json.load(open(sys.argv[1]))["baseline"]
print("Qualification", a["observed_rps"], "RPS,",
      a["p_request_phase_metrics"]["queue"]["mean_ms"], "ms mean P queue,",
      [window["completed"] for window in a["arrival_windows"]], "completions per window")
PY
    mode=baseline
else
    mode=afd
fi
warmup "$group-warmup"
measure "$group-formal" formal-candidate "$mode"
