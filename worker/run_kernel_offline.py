"""Local verification wrapper: inherited kernel network denial, fail closed."""
import ctypes
import runpy
import os
from pathlib import Path
import sys


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
ctx = lib.seccomp_init(0x7fff0000)  # Default allow; add only network denials.
if not ctx:
    raise SystemExit("Could not create network-denial filter; tests not run")
try:
    for name in (b"socket", b"socketpair", b"connect", b"sendto", b"sendmsg", b"sendmmsg"):
        number = lib.seccomp_syscall_resolve_name(name)
        if number < 0 or lib.seccomp_rule_add(ctx, 0x00050001, number, 0):
            raise SystemExit("Could not add network-denial rule; tests not run")
    if lib.seccomp_load(ctx):
        raise SystemExit("Could not load network-denial filter; tests not run")
finally:
    lib.seccomp_release(ctx)

print("Kernel network-denial filter installed (inherited by subprocesses)", flush=True)
os.chdir(Path(__file__).resolve().parent)
sys.argv = ["run_offline_tests.py", *sys.argv[1:]]
runpy.run_path("run_offline_tests.py", run_name="__main__")
