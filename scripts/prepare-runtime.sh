#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "$0")/environment.sh"
if [[ -n "${CASE_RUN_DIR:-}" ]]; then
    exec > >(tee "$CASE_RUN_DIR/prepare.log") 2>&1
fi

value() {
    python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["runtime"][sys.argv[2]])' "$case_dir/case.json" "$1"
}
test "$(git -C "$VLLM_SOURCE" rev-parse HEAD)" = "$(value vllm)"
test "$(git -C "$ASCEND_SOURCE" rev-parse HEAD)" = "$(value vllm_ascend)"
test -r "$CANN_ROOT/set_env.sh"
set +u
source "$CANN_ROOT/set_env.sh"
set -u

mkdir -p "$runtime"
afd="$runtime/afd-plugin"
if [[ ! -d "$afd/.git" ]]; then
    staging=$(mktemp -d "$runtime/afd.XXXXXX")
    trap 'rm -rf -- "$staging"' EXIT
    git -C "$staging" init -q
    git -C "$staging" fetch --depth 1 https://github.com/vllm-project/afd-plugin.git "$(value afd)"
    git -C "$staging" checkout --detach FETCH_HEAD
    mv -- "$staging" "$afd"
    trap - EXIT
fi
test "$(git -C "$afd" rev-parse HEAD)" = "$(value afd)"

overlay="$runtime/vllm-ascend-cws"
for patch in "$case_dir"/patches/*.patch; do
    python3 -B - "$case_dir/case.json" "$patch" <<'PY'
import hashlib, json, sys
from pathlib import Path
path = Path(sys.argv[2])
expected = json.loads(Path(sys.argv[1]).read_text())["runtime"]["patches"][path.name]
if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
    raise SystemExit(f"Patch checksum does not match case lock: {path.name}")
PY
done
if [[ ! -d "$overlay" ]]; then
    staging=$(mktemp -d "$runtime/cws.XXXXXX")
    trap 'rm -rf -- "$staging"' EXIT
    tar -C "$ASCEND_SOURCE" --exclude=.git --exclude=__pycache__ --exclude=./csrc/build --exclude=./csrc/build_out -cf - . | tar -C "$staging" -xf -
    git -C "$staging" apply "$case_dir/patches/cws-v030-mrv2.patch"
    git -C "$staging" apply "$case_dir/patches/cws-pd-logical-pages.patch"
    mv -- "$staging" "$overlay"
    trap - EXIT
fi
git -C "$overlay" apply --check --reverse "$case_dir/patches/cws-v030-mrv2.patch"
git -C "$overlay" apply --check --reverse "$case_dir/patches/cws-pd-logical-pages.patch"

export PYTHONPATH="$afd:$overlay:$VLLM_SOURCE:${PYTHONPATH:-}"
if [[ -z "${CWS_VENDOR_DIR:-}" ]]; then
    CWS_VENDOR_DIR="$runtime/cws-native/vendors/cws32k_transformer"
    if [[ ! -f "$CWS_VENDOR_DIR/op_api/lib/libcust_opapi.so" ]]; then
        (
            cd "$overlay/csrc"
            bash build.sh --pkg --ops=compressor,sparse_attn_sharedkv --soc=ascend910_93 --vendor_name=cws32k -j8 \
                --cann_3rd_lib_path="${CANN_3RD_LIB_PATH:-$overlay/csrc/third_party}"
            shopt -s nullglob
            installers=(build/cann-ops-transformer*.run)
            if [[ ${#installers[@]} -ne 1 ]]; then
                echo "Expected one CWS installer in $PWD/build" >&2
                exit 1
            fi
            mkdir -p "$runtime/cws-native"
            bash "${installers[0]}" --quiet --install-path="$runtime/cws-native"
        )
    fi
else
    supplied_hash=$(sha256sum "$CWS_VENDOR_DIR/op_api/lib/libcust_opapi.so" | cut -d ' ' -f 1)
    test "$supplied_hash" = "$(value cws_reference_opapi_sha256)" || {
        echo "The supplied CWS vendor does not match the reference library hash." >&2
        exit 1
    }
fi
test -s "$CWS_VENDOR_DIR/op_api/lib/libcust_opapi.so"
test "$(basename -- "$CWS_VENDOR_DIR")" = cws32k_transformer
printf '%s\n' "$CWS_VENDOR_DIR" > "$runtime/cws-vendor.path"
if [[ ! -d "$runtime/skill" ]]; then
    staging=$(mktemp -d "$runtime/skill.XXXXXX")
    trap 'rm -rf -- "$staging"' EXIT
    python3 -B "$root/scripts/prepare-runtime.py" \
        "$afd/.agents/skills/deploy-dsv4-flash-async/scripts" "$staging/scripts"
    mv -- "$staging/scripts" "$runtime/skill"
    rmdir "$staging"
    trap - EXIT
fi

# Install into the P container. The runtime tree remains isolated from the image source.
(
    cd "$afd"
    SETUPTOOLS_SCM_PRETEND_VERSION_FOR_VLLM_AFD_PLUGIN=0.30.0rc1.dev11+gbc80959 \
        SOC_VERSION=910c AFD_BUILD_ASCEND_OPS=1 \
        python3 -m pip install --no-deps --no-build-isolation -e .
)
sha256sum "$CWS_VENDOR_DIR/op_api/lib/libcust_opapi.so" \
    "$afd/afd_plugin/_cann_ops_custom/vendors/afd-plugin/op_api/lib/libcust_opapi.so"
