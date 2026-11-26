"""Prepare the mounted data directory, then drop privileges before serving."""
import os
from pathlib import Path
import sys


def select_command(argv, env):
    """WORKER_RUNTIME=serve-paper upgrades the hosted `serve-observe` start command.

    Render's start command (dockerCommand) can only be edited in its dashboard,
    so this one env value selects serve-paper, which still runs every paper gate
    (PAPER_TRADING_ENABLED, reviewed hash, accounting, kill switch). It never
    selects anything else; unknown values refuse to start. Unset = unchanged.
    """
    argv = list(argv) or ["serve"]
    runtime = (env.get("WORKER_RUNTIME") or "").strip()
    if runtime in ("", "serve-observe"):
        return argv
    if runtime != "serve-paper":
        raise SystemExit("WORKER_RUNTIME must be serve-observe or serve-paper")
    if argv[:1] == ["serve-observe"]:
        print("WORKER_RUNTIME=serve-paper: starting serve-paper instead of serve-observe", flush=True)
        return ["serve-paper", *argv[1:]]
    return argv


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
    return cli(select_command(sys.argv[1:], os.environ))


if __name__ == "__main__":
    raise SystemExit(main())

