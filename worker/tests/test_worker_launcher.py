"""Exercise the executable observation launcher without starting the worker."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


WORKER_ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = WORKER_ROOT / "bin" / "serve-observe"


class LauncherTests(unittest.TestCase):
    def run_launcher(self, *args):
        with tempfile.TemporaryDirectory(prefix="momentum-launcher-") as directory:
            # Scope these tests to executable/argv forwarding. This sandbox
            # cannot chown to the container UID; disk ownership and privilege
            # dropping need a real container test. The stub is child-only.
            Path(directory, "sitecustomize.py").write_text(
                "import os\nos.geteuid = lambda: 10001\n", encoding="utf-8",
            )
            # No inherited broker/cloud credentials or runtime configuration.
            env = {
                "PATH": str(Path(sys.executable).parent) + os.pathsep + os.defpath,
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONPATH": directory,
                "MOMENTUM_MODE": "paper",
                "PAPER_TRADING_ENABLED": "false",
                "JOURNAL_PATH": str(Path(directory) / "data" / "journal.sqlite3"),
                "EXPORT_DIR": str(Path(directory) / "exports"),
            }
            result = subprocess.run(
                [str(LAUNCHER), *args], cwd=WORKER_ROOT, env=env,
                capture_output=True, text=True, timeout=10,
            )
            self.assertFalse(Path(env["JOURNAL_PATH"]).exists())
            return result

    def test_executable_reaches_observation_help(self):
        result = self.run_launcher("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("serve-observe [-h]", result.stdout)
        self.assertEqual(result.stderr, "")

    def test_argument_with_spaces_is_forwarded_as_one_argument(self):
        # Unquoted shell forwarding would turn this into --help and exit zero.
        result = self.run_launcher("--help trailing")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertIn("unrecognized arguments: --help trailing", result.stderr)


if __name__ == "__main__":
    unittest.main()
