#!/usr/bin/env bash
set -euo pipefail

root=$(cd -- "$(dirname -- "$0")/.." && pwd)
role=${1:?Specify p0, p1, decode, proxy, proxy-afd, proxy-p0, proxy-p1, or afd}
run_name=${2:?Specify the run directory name}
if [[ ! "$run_name" =~ ^[a-zA-Z0-9_-]+$ ]]; then
    printf '%s\n' "Invalid run directory name: $run_name" >&2
    exit 2
fi

source "$root/scripts/environment.sh"
if [[ -r "$CANN_ROOT/set_env.sh" ]]; then
    set +u
    source "$CANN_ROOT/set_env.sh"
    set -u
fi
case "$role" in
    decode) export NIC_NAME="$D_NIC" DSV4_MODEL="$D_MODEL_DIR" ;;
    *) export NIC_NAME="$P_NIC" DSV4_MODEL="$P_MODEL_DIR" ;;
esac
export PYTHON_VLLM_PATH="$VLLM_SOURCE"
export PYTHON_VLLM_ASCEND_PATH="$ASCEND_SOURCE"
export PYTHON_AFD_PLUGIN_PATH="$runtime/afd-plugin"
export PYTHONPATH="$PYTHON_VLLM_PATH:$PYTHON_VLLM_ASCEND_PATH:${PYTHONPATH:-}"
export DSV4_SCRIPT_DIR="$runtime/skill"
run_root=$(cd -- "${CASE_RUN_DIR:?Set CASE_RUN_DIR}" && pwd -P)
export LOG_DIR="$run_root/$run_name"
export PID_DIR="$LOG_DIR/pids"
mkdir -p "$PID_DIR"
export VLLM_USE_V2_MODEL_RUNNER=1
export OMP_NUM_THREADS=4
export DECODE_VISIBLE_DEVICES="$D_DEVICES"
IFS=, read -r -a p_devices <<< "$P_DEVICES"

devices() {
    local start=$1 count=$2
    local -a subset=("${p_devices[@]:start:count}")
    local IFS=,
    printf '%s\n' "${subset[*]}"
}

select_cws() {
    export ACCEPTANCE_CWS=1
    export PYTHON_VLLM_ASCEND_PATH="$runtime/vllm-ascend-cws"
    local cws_vendor
    cws_vendor=$(<"$runtime/cws-vendor.path")
    local ascend_vendor="$PYTHON_VLLM_ASCEND_PATH/vllm_ascend/_cann_ops_custom/vendors/custom_transformer"
    local opp="$cws_vendor:$ascend_vendor"
    local libs="$cws_vendor/op_api/lib:$ascend_vendor/op_api/lib"
    if [[ $role == afd ]]; then
        local afd_vendor="$PYTHON_AFD_PLUGIN_PATH/afd_plugin/_cann_ops_custom/vendors/afd-plugin"
        opp="$opp:$afd_vendor"
        libs="$libs:$afd_vendor/op_api/lib"
    fi
    export ASCEND_CUSTOM_OPP_PATH="$opp${ASCEND_CUSTOM_OPP_PATH:+:$ASCEND_CUSTOM_OPP_PATH}"
    export LD_LIBRARY_PATH="$libs${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
}

record_process() {
    local label=$1
    shift
    local record="$PID_DIR/$label.pid"
    if [[ -s "$record" ]] && kill -0 "$(cat "$record")" 2>/dev/null; then
        printf '%s\n' "Run already has a live $label process" >&2
        return 1
    fi
    nohup "$@" > "$LOG_DIR/$label.log" 2>&1 < /dev/null &
    printf '%s\n' "$!" > "$record"
    printf 'Started %s pid=%s log=%s\n' "$label" "$!" "$LOG_DIR/$label.log"
}

case "$role" in
    p0|p1)
        export FULL_DP_SIZE=2 FULL_DP_SIZE_LOCAL=2 FULL_DP_START_RANK=0 FULL_HEADLESS=0 FULL_TP_SIZE=4
        export PREFILL_MAX_NUM_BATCHED_TOKENS="$BASELINE_MBT"
        export PREFILL_MAX_NUM_SEQS="$BASELINE_MAX_SEQS" PREFILL_GPU_MEMORY_UTILIZATION="$BASELINE_UTILIZATION"
        export MODEL_LOADER_THREADS=8 OMP_NUM_THREADS=4
        export HCCL_OP_EXPANSION_MODE=AIV VLLM_ASCEND_ENABLE_FUSED_MC2=1
        select_cws
        if [[ "$role" == p0 ]]; then
            export PREFILL_PORT=7100 PREFILL_DP_RPC_PORT=12321 PD_KV_PREFILL_PORT=30000
            export PD_KV_PREFILL_ENGINE_ID=0 FULL_VISIBLE_DEVICES="$(devices 0 8)"
        else
            export PREFILL_PORT=7101 PREFILL_DP_RPC_PORT=12421 PD_KV_PREFILL_PORT=30200
            export PD_KV_PREFILL_ENGINE_ID=2 FULL_VISIBLE_DEVICES="$(devices 8 8)"
        fi
        record_process "$role" bash "$DSV4_SCRIPT_DIR/run_prefill_full_rank.sh"
        ;;
    decode)
        case "${3:?Specify producer baseline, afd12, or afd8}" in
            baseline) export PREFILL_DP_SIZE=2 PREFILL_TP_SIZE=4 ;;
            afd12) export PREFILL_DP_SIZE=6 PREFILL_TP_SIZE=2 ;;
            afd8) export PREFILL_DP_SIZE=4 PREFILL_TP_SIZE=2 ;;
            *) echo "Unknown producer $3" >&2; exit 2 ;;
        esac
        # Verified D configuration from the 2026-10-20 PD smoke on these commits.
        unset VLLM_USE_V2_MODEL_RUNNER
        export MODEL_LOADER_THREADS=16
        export HCCL_BUFFSIZE=256 HCCL_CONNECT_TIMEOUT=180 HCCL_EXEC_TIMEOUT=300
        export VLLM_RPC_TIMEOUT=600000 VLLM_EXECUTE_MODEL_TIMEOUT_SECONDS=600
        export OMP_NUM_THREADS=4
        bash "$DSV4_SCRIPT_DIR/run_decode.sh"
        ;;
    proxy|proxy-afd|proxy-p0|proxy-p1)
        case "$role" in
            proxy)
                proxy_port=8000
                prefiller_hosts=("$P_NODE_IP" "$P_NODE_IP")
                prefiller_ports=(7100 7101)
                ;;
            proxy-afd|proxy-p0|proxy-p1)
                proxy_port=8000
                prefiller_hosts=("$P_NODE_IP")
                prefiller_ports=(7100)
                if [[ "$role" == proxy-p0 ]]; then
                    proxy_port=8001
                elif [[ "$role" == proxy-p1 ]]; then
                    proxy_port=8002
                    prefiller_ports=(7101)
                fi
                ;;
        esac
        proxy_script="$PYTHON_VLLM_ASCEND_PATH/examples/disaggregated_prefill_v1/load_balance_proxy_server_example.py"
        record_process "$role" python3 "$proxy_script" \
            --host 0.0.0.0 --port "$proxy_port" \
            --prefiller-hosts "${prefiller_hosts[@]}" --prefiller-ports "${prefiller_ports[@]}" \
            --decoder-hosts "$D_NODE_IP" --decoder-ports 7100
        ;;
    afd)
        case "${3:?Specify 12a4f or 8a8f}" in
            12a4f)
                export PREFILL_DP_SIZE=6 NUM_ATTENTION_RANKS=12
                export FFN_DP_SIZE=4 NUM_FFN_RANKS=4
                export PREFILL_ATTN_VISIBLE_DEVICES="$(devices 0 12)"
                export PREFILL_FFN_VISIBLE_DEVICES="$(devices 12 4)"
                ;;
            8a8f)
                export PREFILL_DP_SIZE=4 NUM_ATTENTION_RANKS=8
                export FFN_DP_SIZE=8 NUM_FFN_RANKS=8
                export PREFILL_ATTN_VISIBLE_DEVICES="$(devices 0 8)"
                export PREFILL_FFN_VISIBLE_DEVICES="$(devices 8 8)"
                ;;
            *)
                printf '%s\n' 'Specify 12a4f or 8a8f' >&2
                exit 2
                ;;
        esac
        export PREFILL_TP_SIZE=2 ATTN_RANKS_PER_DP=2
        export PREFILL_MAX_NUM_BATCHED_TOKENS="$AFD_MBT" PREFILL_MAX_NUM_SEQS="$AFD_MAX_SEQS"
        export PREFILL_GPU_MEMORY_UTILIZATION="$AFD_UTILIZATION"
        export MODEL_LOADER_THREADS=16 OMP_NUM_THREADS=4
        select_cws
        bash "$DSV4_SCRIPT_DIR/run_prefill.sh"
        ;;
    *)
        printf '%s\n' "Unknown role: $role" >&2
        exit 2
        ;;
esac
