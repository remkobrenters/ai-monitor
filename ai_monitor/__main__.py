"""CLI, passive hook entry point, and GNOME's persistent JSON bridge."""

import argparse
import json
import os
from pathlib import Path
import select
import sys
import time

from .client import request


def watch():
    active = False
    buffer = b""
    while True:
        try:
            snapshot = request({"op": "snapshot", "active": active})
            print(json.dumps({"type": "snapshot", **snapshot}), flush=True)
        except (OSError, ValueError):
            print(json.dumps({"type": "offline", "error": "ai-monitor service is unavailable"}), flush=True)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            ready, _, _ = select.select([sys.stdin], [], [], max(0, deadline - time.monotonic()))
            if not ready:
                break
            chunk = os.read(sys.stdin.fileno(), 65536)
            if not chunk:
                return
            buffer += chunk
            if len(buffer) > 65536:
                return
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                try:
                    message = json.loads(line)
                    if message.get("op") == "active":
                        active = message.get("value") is True
                        continue
                    result = request(message)
                    if message.get("op") == "focus":
                        print(json.dumps({"type": "focus", "id": message.get("id"), "target": result}), flush=True)
                except (OSError, ValueError, TypeError) as error:
                    reason = str(error) if isinstance(error, ValueError) else "Could not complete the action. Check the service and terminal."
                    print(json.dumps({"type": "error", "error": reason}), flush=True)


def main():
    parser = argparse.ArgumentParser(prog="ai-monitor")
    commands = parser.add_subparsers(dest="command", required=True)
    daemon = commands.add_parser("serve", help="Run the local service")
    daemon.add_argument("--no-network", action="store_true")
    hook = commands.add_parser("hook", help="Passive Claude/Codex lifecycle event (reads JSON stdin)")
    hook.add_argument("tool", choices=("claude", "codex"))
    commands.add_parser("watch", help="Persistent JSON bridge used by the GNOME extension")
    commands.add_parser("status", help="Show sessions and usage as JSON")
    commands.add_parser("refresh", help="Rescan usage (provider backoff remains in effect)")
    ack = commands.add_parser("ack", help="Silence attention for one session or all sessions")
    ack.add_argument("id", nargs="?")
    run = commands.add_parser("run", help="Launch a CLI normally, with a readable session name")
    run.add_argument("--name", required=True)
    run.add_argument("tool", choices=("claude", "codex"))
    run.add_argument("args", nargs=argparse.REMAINDER)
    uninstall = commands.add_parser("uninstall", help="Remove ai-monitor, its service and owned hooks")
    uninstall.add_argument("--purge", action="store_true", help="Also remove history and preferences")
    uninstall.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.command == "serve":
        from .service import serve
        serve(network=not args.no_network)
    elif args.command == "hook":
        from .hook import report
        report(args.tool)
    elif args.command == "watch":
        watch()
    elif args.command == "uninstall":
        manager = Path(__file__).resolve().parents[1] / "scripts/manage.py"
        flags = (["--purge"] if args.purge else []) + (["--dry-run"] if args.dry_run else [])
        os.execv("/usr/bin/python3", ["/usr/bin/python3", str(manager), "uninstall", *flags])
    elif args.command == "run":
        os.environ["AI_MONITOR_NAME"] = args.name
        os.execvp(args.tool, [args.tool, *args.args])
    else:
        try:
            value = request({"op": "snapshot" if args.command == "status" else args.command,
                             **({"id": args.id} if args.command == "ack" else {})})
            print(json.dumps(value, indent=2))
        except (OSError, ValueError) as error:
            parser.exit(1, f"ai-monitor: {error}. Check: systemctl --user status ai-monitor.service\n")


if __name__ == "__main__":
    main()
