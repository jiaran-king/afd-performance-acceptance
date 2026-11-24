# Source from site-specific commands, using this checkout for locked settings.
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
source "${SITE_FILE:?Set SITE_FILE to a readable copy of site.env}"
case_dir="$root/cases/910c-dsv4-w4a8-rps4"
runtime="$root/.runtime"
: "${P_NODE_IP:?Set the P container address}" "${D_NODE_IP:?Set the D container address}"
: "${P_MODEL_DIR:?Set the P model mount}" "${D_MODEL_DIR:?Set the D model mount}"
: "${P_DEVICES:?List the P devices in logical rank order}" "${D_DEVICES:?List the D devices}"
VLLM_SOURCE=${VLLM_SOURCE:-/vllm-workspace/vllm}
ASCEND_SOURCE=${ASCEND_SOURCE:-/vllm-workspace/vllm-ascend}
CANN_ROOT=${CANN_ROOT:-/usr/local/Ascend/cann}
source "$case_dir/config.env"
export P_NODE_IP D_NODE_IP
