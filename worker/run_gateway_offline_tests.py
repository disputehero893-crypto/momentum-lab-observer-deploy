"""Run customer gateway and worker lifecycle regressions with network denied.

Requires Linux and libseccomp.so.2. Missing guards abort before test imports.
Fixtures supply fake Auth/DB/broker transports; no credentials are needed.
"""
import ctypes
from pathlib import Path
import sys


def deny_network_syscalls():
    lib = ctypes.CDLL("libseccomp.so.2", use_errno=True)
    lib.seccomp_init.argtypes = [ctypes.c_uint32]
    lib.seccomp_init.restype = ctypes.c_void_p
    lib.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
    lib.seccomp_rule_add.restype = ctypes.c_int
    lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    lib.seccomp_syscall_resolve_name.restype = ctypes.c_int
    lib.seccomp_load.argtypes = [ctypes.c_void_p]
    lib.seccomp_load.restype = ctypes.c_int
    lib.seccomp_release.argtypes = [ctypes.c_void_p]
    context = lib.seccomp_init(0x7fff0000)
    if not context:
        raise RuntimeError("Network-denial filter unavailable; tests not run")
    try:
        for name in (b"socket", b"socketpair", b"connect", b"sendto", b"sendmsg", b"sendmmsg"):
            number = lib.seccomp_syscall_resolve_name(name)
            if number < 0 or lib.seccomp_rule_add(context, 0x00050001, number, 0):
                raise RuntimeError("Network-denial rule unavailable; tests not run")
        if lib.seccomp_load(context):
            raise RuntimeError("Network-denial filter failed; tests not run")
    finally:
        lib.seccomp_release(context)


if __name__ == "__main__":
    deny_network_syscalls()
    print("Kernel network-denial filter installed (inherited by subprocesses)", flush=True)
    attempts = []

    def audit(event, args):
        if event.startswith("socket.") or event == "urllib.Request":
            attempts.append(event)  # Event names only; never log credentials or URLs.
            raise RuntimeError("Network forbidden during gateway tests")

    sys.addaudithook(audit)
    root = Path(__file__).resolve().parent
    sys.path[:0] = [str(root), str(root / "tests")]
    import unittest

    suite = unittest.defaultTestLoader.loadTestsFromNames([
        "test_customer_gateway", "test_gateway_pending_activation",
        "test_activation_cycle_binding", "test_autopilot", "test_autopilot_api",
        "test_customer_settings",
    ])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if attempts:
        print("Offline network guard: blocked attempts: " + ", ".join(attempts), file=sys.stderr)
        raise SystemExit(1)
    print("Offline network guard: zero HTTP/DNS/socket attempts", flush=True)
    raise SystemExit(not result.wasSuccessful())
