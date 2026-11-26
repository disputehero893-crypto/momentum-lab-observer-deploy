"""Run unittest with HTTP, DNS, and socket access denied before discovery.

Use from worker/: python run_offline_tests.py
Optional arguments have the same syntax as python -m unittest.
"""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from offline_network import install_process_guard


if __name__ == "__main__":
    guard = install_process_guard()
    program = unittest.main(
        module=None,
        argv=[sys.argv[0], *(sys.argv[1:] or ["discover", "-s", "tests", "-v"])],
        exit=False,
    )
    guard.assert_no_attempts()
    print("Offline network guard: zero HTTP/DNS/socket attempts", flush=True)
    raise SystemExit(not program.result.wasSuccessful())
