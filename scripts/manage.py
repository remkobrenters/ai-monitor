#!/usr/bin/env python3
"""Install/uninstall only ai-monitor's files and hook handlers, without sudo or pip."""

import argparse
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ai_monitor.paths import private_json

UUID = "ai-monitor@local"
EVENTS = {"claude": ("SessionStart", "SessionEnd", "UserPromptSubmit", "PreToolUse", "PostToolUse",
                     "PostToolUseFailure", "PermissionRequest", "Notification", "Stop", "SubagentStart", "SubagentStop"),
          "codex": ("SessionStart", "SessionEnd", "UserPromptSubmit", "PreToolUse", "PostToolUse",
                    "PermissionRequest", "Stop", "Interrupt", "SubagentStart", "SubagentStop")}


def run(*args, check=True):
    return subprocess.run(args, check=check, text=True, capture_output=True)


def read_object(path):
    if path.is_symlink():
        raise ValueError(f"Refusing to replace a symlink: {path}")
    if not path.exists():
        return {}
    value = json.loads(path.read_text())
    if not isinstance(value, dict) or not isinstance(value.get("hooks", {}), dict):
        raise ValueError(f"Expected a settings object with a hooks object: {path}")
    for groups in value.get("hooks", {}).values():
        if not isinstance(groups, list) or any(not isinstance(g, dict) or not isinstance(g.get("hooks"), list)
                                               for g in groups):
            raise ValueError(f"Unsupported hooks format: {path}")
    return value


def remove_handlers(settings, command):
    hooks = settings.get("hooks", {})
    for event in list(hooks):
        groups = []
        for group in hooks[event]:
            kept = [handler for handler in group["hooks"]
                    if not (isinstance(handler, dict) and handler.get("command") == command)]
            if kept:
                groups.append({**group, "hooks": kept})
            elif len(kept) == len(group["hooks"]):
                groups.append(group)
        if groups:
            hooks[event] = groups
        else:
            del hooks[event]
    if not hooks:
        settings.pop("hooks", None)


class Installation:
    def __init__(self, stage=None):
        self.stage = stage
        self.home = stage / "home" if stage else Path.home()
        self.data = stage / "data" if stage else Path(os.environ.get("XDG_DATA_HOME", self.home / ".local/share"))
        self.config = stage / "config" if stage else Path(os.environ.get("XDG_CONFIG_HOME", self.home / ".config"))
        self.state = (stage / "state" if stage else Path(os.environ.get("XDG_STATE_HOME", self.home / ".local/state"))) / "ai-monitor"
        self.app = self.data / "ai-monitor"
        self.extension = self.data / f"gnome-shell/extensions/{UUID}"
        self.launcher = self.home / ".local/bin/ai-monitor"
        self.unit = self.config / "systemd/user/ai-monitor.service"
        self.manifest = self.state / "install.json"
        self.tool_homes = {
            "claude": self.home / ".claude" if stage else Path(os.environ.get("CLAUDE_CONFIG_DIR", self.home / ".claude")),
            "codex": self.home / ".codex" if stage else Path(os.environ.get("CODEX_HOME", self.home / ".codex"))}

    def previous(self):
        if not self.manifest.exists():
            return {"hooks": []}
        value = json.loads(self.manifest.read_text())
        if not isinstance(value, dict) or not isinstance(value.get("hooks"), list):
            raise ValueError("Installation record is damaged; restore it before uninstalling")
        records = []
        for record in value['hooks']:
            if not isinstance(record, dict) or not isinstance(record.get('path'), str) or not isinstance(record.get('command'), str):
                raise ValueError('Invalid hook ownership record')
            if 'original' in record:
                # Migrate old records in memory; never save a copy of unrelated CLI settings again.
                original = json.loads(record['original']) if record['original'] is not None else {}
                record = {'path': record['path'], 'command': record['command'],
                          'created': record['original'] is None, 'emptyHooks': original.get('hooks') == {}}
            records.append({'path': record['path'], 'command': record['command'],
                            'created': bool(record.get('created')), 'emptyHooks': bool(record.get('emptyHooks'))})
        return {'hooks': records}

    def preflight(self):
        if sys.version_info < (3, 11):
            raise ValueError("Python 3.11+ is required")
        if not shutil.which("glib-compile-schemas"):
            raise ValueError("Install libglib2.0-bin first")
        # Never follow a directory symlink while replacing installed files.
        managed = (self.app, self.extension, self.launcher, self.unit, self.state)
        for path in (*managed, *self.tool_homes.values()):
            if any(character in str(path) for character in ('\n', '\r', '\0')):
                raise ValueError('Installation paths must not contain control characters')
        for path in managed:
            for parent in (path, *path.parents):
                if parent.is_symlink():
                    raise ValueError(f"Installation path contains a symlink: {parent}")
        if self.stage:
            return
        if os.getuid() == 0:
            raise ValueError("Run as your desktop user, without sudo")
        if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"):
            raise ValueError("Install from a terminal on your local GNOME desktop")
        version = run("gnome-shell", "--version").stdout.split()[-1].split(".")[0]
        if version not in ("46", "47", "48", "49", "50"):
            raise ValueError("GNOME Shell 46–50 is required")
        if os.environ.get("XDG_SESSION_TYPE") not in ("x11", "wayland"):
            raise ValueError("A GNOME graphical session is required")
        run("systemctl", "--user", "is-active", "--quiet", "graphical-session.target")
        run("/usr/bin/python3", "-c", 'import gi; gi.require_version("Adw", "1"); gi.require_version("Gtk", "4.0"); from gi.repository import Adw, Gtk; assert hasattr(Adw, "SwitchRow") and hasattr(Gtk, "FileDialog")')

    def install(self, hooks=True, dry_run=False):
        self.preflight()
        previous = self.previous()
        records = previous["hooks"]
        planned = []
        if hooks:
            for tool, directory in self.tool_homes.items():
                path = directory / ("settings.json" if tool == "claude" else "hooks.json")
                settings = read_object(path)
                command = f"/usr/bin/python3 {shlex.quote(str(self.app / 'entry.py'))} hook {tool}"
                prior = next((record for record in records if record["path"] == str(path)), None)
                if prior:
                    remove_handlers(settings, prior["command"])
                record = prior or {'path': str(path), 'command': command,
                                   'created': not path.exists(), 'emptyHooks': settings.get('hooks') == {}}
                for event in EVENTS[tool]:
                    settings.setdefault("hooks", {}).setdefault(event, []).append({"hooks": [
                        {"type": "command", "command": command, "timeout": 2}]})
                planned.append((path, settings, {**record, 'command': command}))
        print(f"Application: {self.app}\nExtension: {self.extension}\nCommand: {self.launcher}\nService: {self.unit}")
        for path, _, _ in planned:
            print(f"Merge passive hooks: {path}")
        if dry_run:
            return
        if not self.stage:
            run("gnome-extensions", "disable", UUID, check=False)
            run("systemctl", "--user", "stop", "ai-monitor.service", check=False)
        for path in (self.app, self.extension, self.unit.parent, self.launcher.parent, self.state):
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.state.chmod(0o700)
        # Application directories contain installed code only; replace them so
        # removed assets and old modules do not survive an upgrade.
        for path in (self.app, self.extension):
            shutil.rmtree(path)
            path.mkdir(mode=0o700)
        shutil.copytree(ROOT / "ai_monitor", self.app / "ai_monitor", dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        shutil.copytree(ROOT / "extension", self.extension, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("gschemas.compiled"))
        # Keep uninstall available even after the source checkout/archive is deleted.
        (self.app / "scripts").mkdir(exist_ok=True)
        shutil.copy2(ROOT / "scripts/manage.py", self.app / "scripts/manage.py")
        shutil.copytree(ROOT / "extension/schemas", self.app / "extension/schemas", dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("gschemas.compiled"))
        entry = self.app / "entry.py"
        entry.write_text("from ai_monitor.__main__ import main\nmain()\n")
        self.launcher.write_text(f"#!/bin/sh\nexec /usr/bin/python3 {shlex.quote(str(entry))} \"$@\"\n")
        self.launcher.chmod(0o755)
        quoted = str(entry).replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"')
        environment = []
        for name, value in (("CLAUDE_CONFIG_DIR", self.tool_homes["claude"]), ("CODEX_HOME", self.tool_homes["codex"]),
                            ("XDG_DATA_HOME", self.data), ("XDG_STATE_HOME", self.state.parent)):
            escaped = str(value).replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"')
            environment.append(f'Environment="{name}={escaped}"')
        self.unit.write_text("[Unit]\nDescription=Local AI usage and terminal session monitor\n"
                             "After=graphical-session.target\nPartOf=graphical-session.target\n\n[Service]\n"
                             f'ExecStart=/usr/bin/python3 "{quoted}" serve\n' + "\n".join(environment) + "\n"
                             "Restart=on-failure\nRestartSec=3\nUMask=0077\nRuntimeDirectory=ai-monitor\n"
                             "RuntimeDirectoryMode=0700\nNoNewPrivileges=true\n\n[Install]\nWantedBy=graphical-session.target\n")
        run("glib-compile-schemas", "--strict", str(self.extension / "schemas"))
        # Persist each ownership record before editing settings, so partial installs remain removable.
        for path, settings, record in planned:
            records = [r for r in records if r["path"] != str(path)] + [record]
            private_json(self.manifest, {"hooks": records})
            private_json(path, settings)
        private_json(self.manifest, {"hooks": records})
        if not self.stage:
            run("systemctl", "--user", "daemon-reload")
            run("systemctl", "--user", "enable", "--now", "ai-monitor.service")
            # Append our UUID without enabling globally disabled extensions or changing other extensions.
            run("/usr/bin/python3", "-c", '''from gi.repository import Gio
s = Gio.Settings.new('org.gnome.shell')
uuid = 'ai-monitor@local'
enabled = s.get_strv('enabled-extensions')
if uuid not in enabled: enabled.append(uuid)
assert s.set_strv('enabled-extensions', enabled)
assert s.set_strv('disabled-extensions', [x for x in s.get_strv('disabled-extensions') if x != uuid])
Gio.Settings.sync()
''')
        print("Installed ai-monitor. Log out and back in to load the panel.\n"
              "Restart existing Claude/Codex sessions to load hooks. In Codex, open /hooks and review/trust ai-monitor's hooks.\n"
              "If GNOME extensions are globally disabled, enable them in the Extensions app.\n"
              "Uninstall: ai-monitor uninstall (keeps history); ai-monitor uninstall --purge (also removes history/preferences).")

    def uninstall(self, purge=False, dry_run=False):
        # Validate all settings before removing any application files.
        records = self.previous()["hooks"]
        planned = []
        for record in records:
            path = Path(record["path"])
            if not path.exists():
                continue
            settings = read_object(path)
            remove_handlers(settings, record["command"])
            if record.get('emptyHooks') and 'hooks' not in settings:
                settings['hooks'] = {}
            planned.append((path, settings, record.get('created', False)))
        print("Remove ai-monitor's hooks, service, command and extension" + ("; purge history/preferences" if purge else "; keep history/preferences"))
        if dry_run:
            return
        for path, settings, created in planned:
            if created and not settings:
                path.unlink()
            else:
                private_json(path, settings)
        if not self.stage:
            run("gnome-extensions", "disable", UUID, check=False)
            run("systemctl", "--user", "disable", "--now", "ai-monitor.service", check=self.unit.exists())
            if purge:
                # Also works after a previous non-purge uninstall removed the installed schema.
                with tempfile.TemporaryDirectory(prefix="ai-monitor-schema-") as temporary:
                    schema = ROOT / "extension/schemas/org.gnome.shell.extensions.ai-monitor.gschema.xml"
                    shutil.copy2(schema, temporary)
                    run("glib-compile-schemas", "--strict", temporary)
                    run("gsettings", "--schemadir", temporary, "reset-recursively",
                        "org.gnome.shell.extensions.ai-monitor")
            run("/usr/bin/python3", "-c", '''from gi.repository import Gio
s = Gio.Settings.new('org.gnome.shell')
for key in ('enabled-extensions', 'disabled-extensions'):
    assert s.set_strv(key, [x for x in s.get_strv(key) if x != 'ai-monitor@local'])
Gio.Settings.sync()
''')
        for path in (self.app, self.extension):
            if path.is_symlink():
                path.unlink()
            elif path.exists():
                shutil.rmtree(path)
        self.unit.unlink(missing_ok=True)
        self.launcher.unlink(missing_ok=True)
        self.manifest.unlink(missing_ok=True)
        if purge and self.state.exists():
            shutil.rmtree(self.state)
        if not self.stage:
            run("systemctl", "--user", "daemon-reload")
        print("Uninstalled ai-monitor.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "uninstall"))
    parser.add_argument("--no-hooks", action="store_true", help="Install without changing Claude/Codex settings")
    parser.add_argument("--purge", action="store_true", help="Also delete ai-monitor history and preferences")
    parser.add_argument("--dry-run", action="store_true", help="Validate and print paths without writing")
    parser.add_argument("--destdir", type=Path, help="Stage an isolated installation; never touch desktop/services")
    args = parser.parse_args()
    try:
        installation = Installation(args.destdir.resolve() if args.destdir else None)
        if args.action == "install":
            installation.install(hooks=not args.no_hooks, dry_run=args.dry_run)
        else:
            installation.uninstall(purge=args.purge, dry_run=args.dry_run)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"ai-monitor: {error}\n")


if __name__ == "__main__":
    main()
