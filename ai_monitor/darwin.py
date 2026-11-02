"""macOS process identity through libproc and sysctl, the sources ps(1) and lsof(8) use."""

import ctypes
import functools
import os
import struct

PROC_PIDTBSDINFO = 3
PROC_PIDVNODEPATHINFO = 9
BSDINFO = struct.Struct("<12I16s32s5IiQQ")
BSDINFO_SIZE = 136
VNODEPATHINFO_SIZE = 2352
CWD_OFFSET = 152  # struct vnode_info precedes the current directory's path.
MAXPATHLEN = 1024
SZOMB = 5
NODEV = 0xFFFFFFFF
S_IFCHR = 0o020000
CTL_KERN, KERN_ARGMAX, KERN_PROCARGS2 = 1, 8, 49


# Loaded on first use, so the parsers below can be imported and tested on any platform.
@functools.cache
def _libc():
    libc = ctypes.CDLL(None, use_errno=True)
    libc.devname.restype = ctypes.c_char_p
    libc.devname.argtypes = (ctypes.c_uint32, ctypes.c_uint16)
    return libc


@functools.cache
def _libproc():
    return ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)


def _bsdinfo(pid):
    buffer = ctypes.create_string_buffer(BSDINFO_SIZE)
    if _libproc().proc_pidinfo(int(pid), PROC_PIDTBSDINFO, 0, buffer, BSDINFO_SIZE) != BSDINFO_SIZE:
        return None
    fields = BSDINFO.unpack_from(buffer.raw)
    return {"status": fields[1], "pid": fields[3], "parent": fields[4], "uid": fields[5],
            "tdev": fields[17], "start": f"{fields[20]}.{fields[21]:06d}"}


def process(pid):
    info = _bsdinfo(pid)
    if not info or info["status"] == SZOMB:
        return None
    return {"pid": info["pid"], "parent": info["parent"], "start": info["start"]}


def uid(pid):
    info = _bsdinfo(pid)
    return info["uid"] if info else None


def started(identity):
    """Wall-clock start time encoded in a process identity."""
    return float(identity["start"])


def tty(pid):
    info = _bsdinfo(pid)
    if not info or info["tdev"] == NODEV:
        return ""
    name = _libc().devname(info["tdev"], S_IFCHR)
    return f"/dev/{name.decode()}" if name else ""


def pids():
    count = _libproc().proc_listallpids(None, 0)
    if count <= 0:
        return []
    values = (ctypes.c_int * (count + 64))()
    count = _libproc().proc_listallpids(values, ctypes.sizeof(values))
    return [pid for pid in values[:max(0, count)] if pid > 0]


def executable(pid):
    buffer = ctypes.create_string_buffer(4 * MAXPATHLEN)
    if _libproc().proc_pidpath(int(pid), buffer, len(buffer)) <= 0:
        raise OSError(ctypes.get_errno(), "Process path unavailable")
    return os.fsdecode(buffer.value)


def cwd(pid):
    buffer = ctypes.create_string_buffer(VNODEPATHINFO_SIZE)
    if _libproc().proc_pidinfo(int(pid), PROC_PIDVNODEPATHINFO, 0, buffer, VNODEPATHINFO_SIZE) != VNODEPATHINFO_SIZE:
        raise OSError(ctypes.get_errno(), "Process directory unavailable")
    path = buffer.raw[CWD_OFFSET:CWD_OFFSET + MAXPATHLEN].split(b"\0", 1)[0]
    if not path:
        raise OSError("Process directory unavailable")
    return os.fsdecode(path)


def parse_procargs(raw):
    """KERN_PROCARGS2: argc, the exec path, NUL padding, argv, then the environment."""
    if len(raw) < 4:
        raise OSError("Process arguments unavailable")
    argc = struct.unpack_from("<i", raw)[0]
    _, _, rest = raw[4:].partition(b"\0")
    values = rest.lstrip(b"\0").split(b"\0")
    arguments = values[:argc]
    environment = []
    for entry in values[argc:]:
        if not entry:
            break
        environment.append(entry)
    return arguments, environment


def _procargs(pid):
    size = ctypes.c_size_t(ctypes.sizeof(ctypes.c_int))
    limit = ctypes.c_int()
    mib = (ctypes.c_int * 2)(CTL_KERN, KERN_ARGMAX)
    if _libc().sysctl(mib, 2, ctypes.byref(limit), ctypes.byref(size), None, 0) != 0:
        raise OSError(ctypes.get_errno(), "Argument limit unavailable")
    buffer = ctypes.create_string_buffer(limit.value)
    size = ctypes.c_size_t(limit.value)
    mib = (ctypes.c_int * 3)(CTL_KERN, KERN_PROCARGS2, int(pid))
    if _libc().sysctl(mib, 3, buffer, ctypes.byref(size), None, 0) != 0:
        raise OSError(ctypes.get_errno(), "Process arguments unavailable")
    return parse_procargs(buffer.raw[:size.value])


def arguments(pid):
    return _procargs(pid)[0]


def environment(pid):
    return _procargs(pid)[1]
