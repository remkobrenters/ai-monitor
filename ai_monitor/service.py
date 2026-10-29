"""Local user service: fast session events and independent background usage collection."""

import json
import os
from pathlib import Path
import signal
import shutil
import socket
import socketserver
import struct
import threading
import time

from .paths import socket_path, state_dir
from .sessions import Sessions
from .terminal import alive, discover, focus_target
from .usage import SCAN_INTERVAL, UsageMonitor
from .titles import Titles


class Monitor:
    def __init__(self, directory=None, network=True):
        directory = directory or state_dir()
        self.directory = directory
        self.sessions = Sessions(directory / "sessions.json")
        self.collector = UsageMonitor(Path.home(), directory, network=network)
        self.usage = {"tokens": {}, "limits": {}, "errors": {}, "days": []}
        self.error = ""
        self.last_active = 0
        self.refresh = threading.Event()
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.discovery_lock = threading.Lock()
        self.discovery_at = -10
        self.candidates = []
        self.health = {"eventsReceived": 0, "unlinkedEvents": 0}
        self.titles = Titles()
        self.title_refresh = threading.Event()

    def discover(self, force=False):
        with self.discovery_lock:
            if force or time.monotonic() - self.discovery_at >= 2:
                self.candidates = discover()
                self.sessions.discover(self.candidates)
                self.title_refresh.set()
                self.discovery_at = time.monotonic()
            return self.candidates

    def hook_health(self):
        result = {}
        for tool in ('claude', 'codex'):
            try:
                value = json.loads((self.directory / 'hooks' / f'{tool}.json').read_text())
                if isinstance(value, dict):
                    result[tool] = {key: value.get(key) for key in ('event', 'status', 'at')}
            except (OSError, ValueError):
                pass
        return result

    def dispatch(self, message):
        op = message.get("op")
        if op == "event":
            self.health["eventsReceived"] += 1
            terminal = message.get("terminal", {})
            if not isinstance(terminal, dict):
                raise ValueError('Invalid terminal metadata')
            if not terminal.get("tty") or not alive(terminal.get('process', {})):
                # Recent Codex clients use a background app-server with no controlling tty.
                candidates = self.discover(force=True)
                terminal = self.sessions.resolve_terminal(message, candidates)
                if not terminal:
                    self.health["unlinkedEvents"] += 1
                    return None
                message = {**message, "terminal": terminal}
            self.sessions.event(message)
        elif op == "snapshot":
            self.discover()
            if message.get("active") is True:
                self.last_active = time.monotonic()
            with self.lock:
                return {"sessions": self.sessions.snapshot(), "usage": self.usage,
                        "collectorError": self.error, "heartbeat": time.time(), "health": dict(self.health),
                        "hooks": self.hook_health(),
                        "tools": [tool for tool in ("claude", "codex") if shutil.which(tool, path=os.environ.get("PATH", "") +
                                  os.pathsep + str(Path.home()/'.local/bin') + os.pathsep + str(Path.home()/'.npm-packages/bin'))]}
        elif op == "ack":
            self.sessions.acknowledge(message.get("id"))
        elif op == "focus":
            row = self.sessions.get(message.get("id"))
            return focus_target(row["terminal"])
        elif op == "refresh":
            # Requests only schedule a scan; subscription lookups retain their backoff.
            self.refresh.set()
        else:
            raise ValueError("Unknown operation")
        return None

    def collect(self):
        while not self.stop.is_set():
            self.refresh.clear()
            try:
                now = time.time()
                self.collector.scan(now)
                self.collector.refresh_limits(now, active=time.monotonic() - self.last_active < 30)
                usage = self.collector.snapshot(time.time())
                with self.lock:
                    self.usage, self.error = usage, ""
            except Exception as error:  # Keep session alerts alive even if a provider changes format.
                with self.lock:
                    self.error = f"Usage collection failed ({type(error).__name__}); retrying."
            self.refresh.wait(SCAN_INTERVAL)

    def collect_titles(self):
        # Initial metadata scans can be large; never hold up session events or the panel.
        while not self.stop.is_set():
            if not self.title_refresh.wait(2):
                continue
            self.title_refresh.clear()
            try:
                names = self.titles.lookup(self.sessions.title_records())
                self.sessions.refresh_titles(names)
            except (OSError, ValueError, TypeError):
                pass


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(4)
        if hasattr(socket, "SO_PEERCRED"):
            _, uid, _ = struct.unpack("3i", self.connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid != os.getuid():
                return
        try:
            raw = self.rfile.readline(65537)
            if len(raw) > 65536 or not raw.endswith(b"\n"):
                raise ValueError("Request too large")
            message = json.loads(raw)
            if not isinstance(message, dict):
                raise ValueError("Expected a JSON object")
            result = self.server.monitor.dispatch(message)
            response = {"ok": True, "result": result}
        except ValueError as error:
            response = {"ok": False, "error": str(error)}
        except (TypeError, KeyError, OSError):
            response = {"ok": False, "error": "Request failed; the session may have ended or its terminal is unavailable."}
        try:
            self.wfile.write(json.dumps(response).encode() + b"\n")
        except OSError:
            pass


class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    block_on_close = False


def serve(network=True):
    path = socket_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.is_symlink() or path.parent.stat().st_uid != os.getuid():
        raise RuntimeError("The runtime directory must be owned by this user")
    os.chmod(path.parent, 0o700)
    # systemd serializes restarts. A live existing socket must never be removed.
    if path.exists():
        from .client import request
        try:
            request({"op": "snapshot"}, path=path)
        except (ConnectionRefusedError, FileNotFoundError):
            path.unlink(missing_ok=True)
        else:
            raise RuntimeError("ai-monitor is already running")
    monitor = Monitor(network=network)
    with Server(str(path), Handler) as server:
        os.chmod(path, 0o600)
        server.monitor = monitor
        worker = threading.Thread(target=monitor.collect, daemon=True)
        worker.start()
        threading.Thread(target=monitor.collect_titles, daemon=True).start()

        def finish(*_):
            monitor.stop.set()
            monitor.refresh.set()
            monitor.title_refresh.set()
            threading.Thread(target=server.shutdown, daemon=True).start()

        signal.signal(signal.SIGTERM, finish)
        signal.signal(signal.SIGINT, finish)
        try:
            server.serve_forever(poll_interval=0.25)
        finally:
            path.unlink(missing_ok=True)
