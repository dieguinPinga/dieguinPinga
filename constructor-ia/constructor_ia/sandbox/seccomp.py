"""Filtro seccomp-BPF (x86_64) generado sin dependencias externas.

Es una capa adicional a Bubblewrap: niega llamadas al sistema que el código de
una tarea Python acotada no necesita y que amplían la superficie de ataque del
kernel (ptrace, bpf, io_uring, keyctl, montajes, módulos, espacios de nombres…).
Las llamadas denegadas devuelven EPERM; una arquitectura distinta mata el proceso.
"""
from __future__ import annotations

import platform
import struct

AUDIT_ARCH_X86_64 = 0xC000003E
X32_SYSCALL_BIT = 0x40000000
RET_KILL_PROCESS = 0x80000000
RET_ERRNO_EPERM = 0x00050000 | 1
RET_ALLOW = 0x7FFF0000

BPF_LD_W_ABS = 0x20
BPF_JEQ_K = 0x15
BPF_JGE_K = 0x35
BPF_RET_K = 0x06

DENIED_X86_64 = {
    "acct": 163, "add_key": 248, "bpf": 321, "chroot": 161, "clock_adjtime": 305, "clock_settime": 227,
    "delete_module": 176, "fanotify_init": 300, "finit_module": 313, "fsconfig": 431, "fsmount": 432,
    "fsopen": 430, "fspick": 433, "init_module": 175, "io_uring_enter": 426, "io_uring_register": 427,
    "io_uring_setup": 425, "ioperm": 173, "iopl": 172, "kcmp": 312, "kexec_file_load": 320, "kexec_load": 246,
    "keyctl": 250, "lookup_dcookie": 212, "mount": 165, "mount_setattr": 442, "move_mount": 429,
    "name_to_handle_at": 303, "open_by_handle_at": 304, "open_tree": 428, "perf_event_open": 298,
    "pivot_root": 155, "process_vm_readv": 310, "process_vm_writev": 311, "ptrace": 101, "quotactl": 179,
    "reboot": 169, "request_key": 249, "setns": 308, "settimeofday": 164, "swapoff": 168, "swapon": 167,
    "syslog": 103, "umount2": 166, "unshare": 272, "uselib": 134, "userfaultfd": 323, "vhangup": 153,
    "adjtimex": 159, "pidfd_getfd": 438, "process_madvise": 440,
}


def supported() -> bool:
    return platform.machine() == "x86_64"


def _ins(code: int, jt: int, jf: int, k: int) -> bytes:
    return struct.pack("=HBBI", code, jt, jf, k)


def build_filter() -> bytes:
    if not supported():
        raise RuntimeError("Filtro seccomp disponible solo para x86_64")
    nrs = sorted(set(DENIED_X86_64.values()))
    prog = [
        _ins(BPF_LD_W_ABS, 0, 0, 4),                      # arch
        _ins(BPF_JEQ_K, 1, 0, AUDIT_ARCH_X86_64),
        _ins(BPF_RET_K, 0, 0, RET_KILL_PROCESS),
        _ins(BPF_LD_W_ABS, 0, 0, 0),                      # nr
    ]
    # Índices finales: ALLOW en E-1, ERRNO en E.
    total = len(prog) + 1 + len(nrs) + 2
    errno_idx = total - 1
    i = len(prog)
    prog.append(_ins(BPF_JGE_K, errno_idx - i - 1, 0, X32_SYSCALL_BIT))
    for nr in nrs:
        i = len(prog)
        prog.append(_ins(BPF_JEQ_K, errno_idx - i - 1, 0, nr))
    prog.append(_ins(BPF_RET_K, 0, 0, RET_ALLOW))
    prog.append(_ins(BPF_RET_K, 0, 0, RET_ERRNO_EPERM))
    assert len(prog) == total
    return b"".join(prog)
