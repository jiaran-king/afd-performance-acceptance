"""Record the custom op libraries mapped by the processes in an AFD run."""

import argparse
import hashlib
import json
import os
from pathlib import Path


def mapped_libraries(maps):
    libraries = set()
    for line in maps.splitlines():
        fields = line.split(maxsplit=5)
        if len(fields) != 6:
            continue
        path = fields[5]
        if "/afd_plugin/_C_ascend." in path or (
            ("/vendors/afd-plugin/" in path or "/vendors/cws32k_transformer/" in path)
            and "/libcust_opapi.so" in path
        ):
            libraries.add(path)
    return sorted(libraries)


def collect(run):
    identity = os.fsencode(f"LOG_DIR={run}")
    processes = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            env = (entry / "environ").read_bytes().split(b"\0")
            if identity not in env:
                continue
            libraries = mapped_libraries((entry / "maps").read_text())
            if not libraries:
                continue
            name = (entry / "comm").read_text().strip()
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
        devices = next(
            (os.fsdecode(e).partition("=")[2] for e in env if e.startswith(b"ASCEND_RT_VISIBLE_DEVICES=")),
            None,
        )
        processes.append(
            {
                "pid": int(entry.name),
                "process": name,
                "visible_devices": devices,
                "mapped_custom_ops": libraries,
            }
        )
    return sorted(processes, key=lambda process: process["pid"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    args = parser.parse_args()
    processes = collect(args.run.resolve(strict=True))
    if not processes:
        raise RuntimeError("No mapped AFD native libraries found for this run")
    paths = {path for process in processes for path in process["mapped_custom_ops"]}
    hashes = {}
    for path in sorted(paths):
        with Path(path).open("rb") as library:
            hashes[path] = hashlib.file_digest(library, "sha256").hexdigest()
    print(json.dumps({"processes": processes, "library_sha256": hashes}, indent=2))


if __name__ == "__main__":
    main()
