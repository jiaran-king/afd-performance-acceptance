#!/usr/bin/env bash
set -euo pipefail

root=$(cd -- "$(dirname -- "$0")" && pwd)
usage() {
    printf '%s\n' \
        "Usage: ./reproduce.sh --verify-reference" \
        "       ./reproduce.sh --site site.env [--prepare-only]"
}

if [[ ${1:-} == --verify-reference && $# == 1 ]]; then
    exec python3 -B "$root/scripts/verify-reference.py"
fi
if [[ ${1:-} == --help ]]; then usage; exit 0; fi
if [[ ${1:-} != --site || $# -lt 2 || $# -gt 3 ]]; then usage >&2; exit 2; fi
site=$2
case "${3:-}" in ""|--prepare-only) ;; *) usage >&2; exit 2 ;; esac
source "$site"

shared=${SHARED_ROOT:-$root}
case "$shared" in /*) ;; *) echo "SHARED_ROOT must be absolute inside both containers." >&2; exit 2 ;; esac
remote_site=${SITE_REMOTE_FILE:-$shared/site.env}
run="$shared/runs/$(date -u +%Y%m%dT%H%M%SZ)-$$"
site_runner=${NODE_EXEC:-}
group=

node() {
    local role=$1 remote_args ssh_target container
    shift
    if [[ -n "$site_runner" ]]; then
        "$site_runner" "$role" "$@"
        return
    fi
    case "$role" in
        P) ssh_target=${P_SSH:-}; container=${P_CONTAINER:-} ;;
        D) ssh_target=${D_SSH:?Set D_SSH or NODE_EXEC}; container=${D_CONTAINER:-} ;;
        *) return 2 ;;
    esac
    if [[ -n "$container" ]]; then
        set -- docker exec "$container" "$@"
    fi
    if [[ -n "$ssh_target" ]]; then
        printf -v remote_args '%q ' "$@"
        ssh -- "$ssh_target" "$remote_args"
    else
        "$@"
    fi
}

run_script() {
    local role=$1 script=$2
    shift 2
    node "$role" env "SITE_FILE=$remote_site" "CASE_RUN_DIR=$run" bash "$shared/scripts/$script" "$@"
}

stop_on() {
    local role=$1 dir=$2
    shift 2
    if node "$role" test -d "$dir"; then
        node "$role" python3 -B "$shared/scripts/stop.py" "$dir" "$@" --wait 45
    fi
}

stop_group() {
    local failed=0
    case "$group" in
        "") return 0 ;;
        baseline)
            stop_on P "$run/baseline-p0-proxy" proxy-p0 || failed=1
            stop_on P "$run/baseline-p1-proxy" proxy-p1 || failed=1
            stop_on P "$run/baseline-proxy" proxy || failed=1
            stop_on P "$run/baseline-p" p0 p1 || failed=1
            ;;
        afd12|afd8)
            stop_on P "$run/$group-proxy" proxy-afd || failed=1
            stop_on P "$run/$group-p" prefill_attention_node0 prefill_ffn_node0 || failed=1
            ;;
    esac
    stop_on D "$run/$group-d" decode || failed=1
    if (( failed )); then return 1; fi
    group=
}

node P test -r "$shared/reproduce.sh"
node D test -r "$shared/reproduce.sh"
node P test -r "$remote_site"
node D test -r "$remote_site"
node P mkdir -p "$shared/runs"
lock="$shared/runs/.active"
if ! node P mkdir "$lock"; then
    echo "A run holds $lock. Check its owner and processes before retrying." >&2
    exit 1
fi
finish() {
    status=$?
    trap - EXIT
    if stop_group; then
        node P rmdir "$lock" || status=1
    else
        echo "Cleanup needs attention. Use the run PID/log directories at $run." >&2
        status=1
    fi
    exit "$status"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
node P mkdir "$run"
node D test -d "$run"

for role in P D; do
    case "$role" in
        P) model=${P_MODEL_DIR:?}; devices=${P_DEVICES:?} ;;
        D) model=${D_MODEL_DIR:?}; devices=${D_DEVICES:?} ;;
    esac
    node "$role" python3 -B "$shared/scripts/preflight.py" "$role" "$model" "$devices" \
        "${VLLM_SOURCE:-/vllm-workspace/vllm}" "${ASCEND_SOURCE:-/vllm-workspace/vllm-ascend}" \
        "${CANN_ROOT:-/usr/local/Ascend/cann}" "${P_NODE_IP:?}" "${D_NODE_IP:?}" "$run/$role-preflight.json"
done

asset_args=()
if [[ -n "${WORKLOAD_ARCHIVE:-}" ]]; then asset_args=(--archive "$WORKLOAD_ARCHIVE"); fi
node P python3 -B "$shared/scripts/prepare-workload.py" "${asset_args[@]+"${asset_args[@]}"}"
run_script P prepare-runtime.sh
if [[ ${3:-} == --prepare-only ]]; then
    printf 'Prepared the locked case at %s\n' "$run"
    exit 0
fi

for arm in baseline afd12 afd8; do
    group=$arm
    run_script D launch.sh decode "$arm-d" "$arm"
    case "$arm" in
        baseline)
            run_script P launch.sh p0 baseline-p
            run_script P launch.sh p1 baseline-p
            run_script P launch.sh proxy baseline-proxy
            run_script P launch.sh proxy-p0 baseline-p0-proxy
            run_script P launch.sh proxy-p1 baseline-p1-proxy
            ;;
        afd12)
            run_script P launch.sh afd afd12-p 12a4f
            run_script P launch.sh proxy-afd afd12-proxy
            ;;
        afd8)
            run_script P launch.sh afd afd8-p 8a8f
            run_script P launch.sh proxy-afd afd8-proxy
            ;;
    esac
    run_script P run-group.sh "$arm"
    stop_group
done

node P python3 -B "$shared/scripts/audit.py" "$shared/.runtime/workload/formal-candidate/requests.jsonl" \
    "$run/baseline-formal" "$run/afd12-formal" \
    --baseline-warmup "$run/baseline-warmup" --afd-warmup "$run/afd12-warmup" --check --output "$run/acceptance.json"
printf 'Artifacts are in %s\n' "$run"
