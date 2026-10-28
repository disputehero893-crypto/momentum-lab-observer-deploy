"""Prepare the mounted data directory, then drop privileges before serving."""
import os
from pathlib import Path
import sys


def main():
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        # Docker starts as root only to initialize the mounted persistent disk.
        for directory in {Path(os.environ.get("JOURNAL_PATH", "/var/data/momentum.sqlite3")).parent,
                          Path(os.environ.get("EXPORT_DIR", "/var/data/exports"))}:
            directory.mkdir(parents=True, exist_ok=True)
            os.chown(directory, 10001, 10001)
        os.setgroups([])
        os.setgid(10001)
        os.setuid(10001)
    from .__main__ import main as cli
    return cli(sys.argv[1:] or ["serve"])


if __name__ == "__main__":
    raise SystemExit(main())

