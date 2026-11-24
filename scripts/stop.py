"""Signal only process leaders with the requested run directory in their environment."""

import argparse
import os
import signal
import time
from pathlib import Path


def stop(run, role):
    if not role.replace("-", "").replace("_", "").isalnum():
        raise ValueError(f"Invalid process role: {role}")
    try:
        pid = int((run / "pids" / f"{role}.pid").read_text().strip())
    except FileNotFoundError:
        return
    if pid <= 1:
        raise ValueError(f"Invalid process ID: {pid}")
    process = Path(f"/proc/{pid}")
    try:
        environment = (process / "environ").read_bytes().split(b"\0")
    except FileNotFoundError:
        print(f"{role} pid={pid} already exited")
        return
    if os.fsencode(f"LOG_DIR={run}") not in environment:
        raise ValueError(f"{role} pid={pid} does not belong to {run}")
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    print(f"{role} pid={pid} sent SIGTERM")


def wait_for_run_exit(run, grace_seconds):
    identity = os.fsencode(f"LOG_DIR={run}")
    deadline = time.monotonic() + grace_seconds
    while True:
        live = []
        for entry in Path("/proc").iterdir():
            if not entry.name.isdecimal():
                continue
            try:
                if identity in (entry / "environ").read_bytes().split(b"\0"):
                    live.append(int(entry.name))
            except (FileNotFoundError, ProcessLookupError, PermissionError):
                continue
        if not live:
            return
        if time.monotonic() >= deadline:
            raise RuntimeError(f"Processes still exiting under {run}: {sorted(live)}")
        time.sleep(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("roles", nargs="+")
    parser.add_argument("--wait", type=int, default=0, help="Grace period for current run workers to exit")
    args = parser.parse_args()
    run = args.run.resolve(strict=True)
    for role in args.roles:
        stop(run, role)
    if args.wait:
        wait_for_run_exit(run, args.wait)


if __name__ == "__main__":
    main()
