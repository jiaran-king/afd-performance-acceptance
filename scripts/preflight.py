"""Check the reference runtime and checkpoint inside a target container."""

import hashlib
import importlib.metadata
import ipaddress
import json
import platform
import re
import socket
import subprocess
import sys
from pathlib import Path


LOCK = Path(__file__).resolve().parent.parent / "cases/910c-dsv4-w4a8-rps4/case.json"


def verify(value, expected, name):
    if value != expected:
        raise ValueError(f"{name}: expected {expected!r}, got {value!r}")


def cmd(*args):
    return subprocess.check_output(args, text=True).strip()


def main():
    role, model, devices, vllm, ascend, cann, p_ip, d_ip, output = sys.argv[1:]
    addresses = {ipaddress.IPv4Address(value) for value in (p_ip, d_ip)}
    verify(len(addresses), 2, "distinct P/D addresses")
    local = p_ip if role == "P" else d_ip
    ports = (7100, 7101, 8000, 8001, 8002) if role == "P" else (7100,)
    for port in ports:
        with socket.socket() as server:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                server.bind((local, port))
            except OSError as error:
                raise ValueError(f"{role} cannot use {local}:{port}: {error}") from error
    lock = json.loads(LOCK.read_text())
    version = lock["runtime"]
    verify(platform.machine(), "aarch64", "container architecture")
    verify(cmd("git", "-C", vllm, "rev-parse", "HEAD"), version["vllm"], "vLLM source")
    verify(cmd("git", "-C", ascend, "rev-parse", "HEAD"), version["vllm_ascend"], "vLLM-Ascend source")
    for source in (vllm, ascend):
        verify(cmd("git", "-C", source, "status", "--short", "--untracked-files=no"), "", f"tracked edits in {source}")
    verify(importlib.metadata.version("torch-npu"), version["torch_npu"], "torch-npu")
    info = (Path(cann) / "compiler/version.info").read_text()
    verify(re.search(r"^Version=(.*)$", info, re.M)[1], version["cann"], "CANN")
    driver_path = Path("/usr/local/Ascend/driver/version.info")
    driver = driver_path.read_text().splitlines()[0] if driver_path.exists() else None

    mapping = cmd("npu-smi", "info", "-m")
    visible = set(re.findall(r"^\s*\d+\s+\d+\s+(\d+)\s+\d+\s+Ascend910\b", mapping, re.M))
    selected = devices.split(",")
    verify(len(selected), 16, f"{role} device rank count")
    verify(len(set(selected)), 16, f"{role} unique devices")
    if not set(selected) <= visible:
        raise ValueError(f"{role} logical devices are outside Ascend device mapping")

    model = Path(model)
    fingerprints = {}
    for name, expected in lock["model"]["file_sha256"].items():
        fingerprints[name] = hashlib.sha256((model / name).read_bytes()).hexdigest()
        verify(fingerprints[name], expected, f"model {name}")
    shards = [p for p in model.glob("*.safetensors") if p.is_file() and p.stat().st_size]
    verify(len(shards), lock["model"]["safetensors_shards"], "safetensors shard count")
    record = json.dumps({
        "role": role, "vllm": version["vllm"], "vllm_ascend": version["vllm_ascend"],
        "cann": version["cann"], "driver": driver, "reference_driver": version["driver"],
        "torch_npu": version["torch_npu"], "devices": selected, "model_sha256": fingerprints,
        "checkpoint_path": str(model.resolve()), "shards": len(shards),
    }, indent=2)
    Path(output).write_text(record + "\n")
    print(record)


if __name__ == "__main__":
    main()
