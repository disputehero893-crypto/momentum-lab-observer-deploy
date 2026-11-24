"""Test-only network denial, including requests hidden by best-effort handlers.

Never store request arguments: those can include credentials. Audit hooks are
installed before discovery or child imports, and remain active until exit.
"""
import atexit
import os
import sys


class NetworkForbidden(AssertionError):
    pass


class NetworkGuard:
    def __init__(self):
        self.attempts = []

    def audit(self, event, _args):
        if event.startswith("socket.") or event == "urllib.Request":
            self.attempts.append(event)
            raise NetworkForbidden("Offline test attempted network access: " + event)

    def assert_no_attempts(self):
        if self.attempts:
            raise NetworkForbidden(
                "Offline test blocked %d network attempt(s): %s"
                % (len(self.attempts), ", ".join(self.attempts))
            )


_process_guard = None


def install_process_guard():
    """Fail at exit even when application code swallowed the rejection."""
    global _process_guard
    if _process_guard is not None:
        return _process_guard
    guard = _process_guard = NetworkGuard()
    sys.addaudithook(guard.audit)

    def check_at_exit():
        try:
            guard.assert_no_attempts()
        except NetworkForbidden as exc:
            print(str(exc), file=sys.stderr, flush=True)
            # sys.exit from atexit is ignored. Ensure child tests cannot turn a
            # swallowed rejection into a successful subprocess result.
            os._exit(1)

    atexit.register(check_at_exit)
    return guard


def offline_pythonpath(*paths):
    """Use the same guard before imports in each tested Python subprocess."""
    tests = os.path.dirname(os.path.abspath(__file__))
    return os.pathsep.join(map(str, (os.path.join(tests, "offline_support"), tests, *paths)))
