"""Shared XDG paths. No credentials or conversations are copied here."""

import json
import os
from pathlib import Path
import sys
import tempfile


def state_dir():
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "ai-monitor"


def socket_path():
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if not runtime and sys.platform == "darwin":
        # macOS has no per-user runtime directory; the private state directory serves instead.
        return state_dir() / "run/control.sock"
    return Path(runtime or f"/run/user/{os.getuid()}") / "ai-monitor/control.sock"


def private_json(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".ai-monitor-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, ensure_ascii=True)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
