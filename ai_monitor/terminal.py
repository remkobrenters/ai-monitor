"""Linux process identity and explicit terminal navigation (never simulated keys)."""

import os
from pathlib import Path
import re
import subprocess

ENVIRONMENT_KEYS = {"TMUX", "TMUX_PANE", "WINDOWID", "SSH_CONNECTION", "SSH_TTY", "AI_MONITOR_NAME",
                    "GNOME_TERMINAL_SERVICE", "GNOME_TERMINAL_SCREEN"}


def process_environment(pid):
    result = {}
    try:
        for entry in (Path('/proc') / str(pid) / 'environ').read_bytes().split(b'\0'):
            key, _, value = entry.partition(b'=')
            name = key.decode(errors='replace')
            if name in ENVIRONMENT_KEYS:
                result[name] = value.decode(errors='replace')
    except OSError:
        pass
    return result


def gnome_target(environment):
    screen = environment.get('GNOME_TERMINAL_SCREEN', '')
    bus = environment.get('GNOME_TERMINAL_SERVICE', '')
    identifier = screen.rsplit('/', 1)[-1].replace('_', '-')
    if not re.fullmatch(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', identifier):
        return None
    if not re.fullmatch(r':[0-9]+\.[0-9]+|org\.gnome\.Terminal(?:\.[A-Za-z0-9_]+)*', bus):
        return None
    return {'busName': bus, 'screenId': identifier}


def process(pid):
    try:
        root = Path("/proc") / str(int(pid))
        fields = (root / "stat").read_text().rsplit(")", 1)[1].split()
        if fields[0] == "Z":
            return None
        return {"pid": int(pid), "parent": int(fields[1]), "start": fields[19]}
    except (OSError, ValueError, IndexError):
        return None


def ancestors(pid):
    result = []
    while pid >= 1 and len(result) < 40:
        info = process(pid)
        if not info or info["pid"] in {p["pid"] for p in result}:
            break
        result.append(info)
        pid = info["parent"]
    return result


def alive(identity):
    if not isinstance(identity, dict):
        return False
    current = process(identity.get("pid", 0))
    return bool(current and current["start"] == identity.get("start"))


def tool_process(tool):
    """Find the CLI ancestor, rather than its short-lived hook shell."""
    for info in ancestors(os.getppid()):
        try:
            root = Path("/proc") / str(info["pid"])
            executable = Path(os.readlink(root / "exe")).name
            command = (root / "cmdline").read_bytes().split(b"\0")[:2]
            names = [Path(arg.decode(errors="replace")).name for arg in command]
            if executable == tool or tool in names or (tool == "codex" and "codex.js" in names):
                return info
        except OSError:
            continue
    return None


def terminal_for(info, environment):
    chain = ancestors(info["pid"])
    environment = dict(environment)
    if not gnome_target(environment):
        for parent in chain[1:]:
            inherited = process_environment(parent['pid'])
            if gnome_target(inherited):
                for key in ('GNOME_TERMINAL_SERVICE', 'GNOME_TERMINAL_SCREEN'):
                    environment[key] = inherited[key]
                break
    tty = ""
    for parent in chain:
        for fd in (0, 1, 2):
            try:
                candidate = os.readlink(f"/proc/{parent['pid']}/fd/{fd}")
                if re.fullmatch(r"/dev/pts/\d+|/dev/tty\d+", candidate):
                    tty = candidate
                    break
            except OSError:
                pass
        if tty:
            break
    tmux = environment.get("TMUX", "").rsplit(",", 2)
    return {"process": info, "ancestors": chain, "tty": tty,
            "tmuxSocket": tmux[0] if len(tmux) == 3 else "",
            "tmuxPane": environment.get("TMUX_PANE", ""),
            "windowId": environment.get("WINDOWID", ""),
            "gnomeTerminal": gnome_target(environment),
            "remote": bool(environment.get("SSH_CONNECTION") or environment.get("SSH_TTY"))}


def capture(tool):
    info = tool_process(tool)
    return terminal_for(info, os.environ) if info else None


def discover():
    """Discover terminal CLI clients, including clients of a detached Codex daemon.

    Only process identity, selected environment keys, and cwd leave this function.
    Command lines are never stored or logged.
    """
    found = []
    for root in Path('/proc').iterdir():
        if not root.name.isdigit():
            continue
        try:
            if root.stat().st_uid != os.getuid():
                continue
            command = (root / "cmdline").read_bytes()[:16384].split(b"\0")
            arguments = [value.decode(errors="replace") for value in command if value]
            names = [Path(arg).name for arg in arguments[:2]]
            tool = next((tool for tool in ("claude", "codex")
                         if tool in names or f"{tool}.js" in names), None)
            if not tool:
                continue
            # Do not count the persistent backend or usage lookups as terminals.
            if tool == "codex" and any(arg in ("app-server", "exec", "features", "doctor", "completion")
                                       for arg in arguments[1:3]):
                continue
            info = process(int(root.name))
            if not info:
                continue
            environment = process_environment(int(root.name))
            terminal = terminal_for(info, environment)
            if not terminal["tty"] or terminal["remote"]:
                continue
            cwd = os.readlink(root / "cwd")
            for index, arg in enumerate(arguments):
                if arg in ("--cd", "-C") and index + 1 < len(arguments):
                    cwd = str((Path(cwd) / arguments[index + 1]).resolve())
                    break
                if arg.startswith("--cd="):
                    cwd = str((Path(cwd) / arg[5:]).resolve())
                    break
            found.append({"tool": tool, "project": cwd, "title": environment.get("AI_MONITOR_NAME", ""),
                          "terminal": terminal})
        except (OSError, ValueError):
            continue
    # npm shims may retain both a node parent and a native child. One CLI per chain.
    return [row for row in found if not any(row["tool"] == other["tool"] and
            row["terminal"]["process"]["pid"] in [p["pid"] for p in other["terminal"]["ancestors"][1:]]
            for other in found if other is not row)]


def recently_started(identity, seconds=15):
    try:
        uptime = float(Path('/proc/uptime').read_text().split()[0])
        age = uptime - int(identity["start"]) / os.sysconf("SC_CLK_TCK")
        return 0 <= age <= seconds
    except (OSError, KeyError, ValueError):
        return False


def run_tmux(socket, *args):
    return subprocess.run(["tmux", "-S", socket, *args], capture_output=True,
                          text=True, timeout=3, check=True).stdout.strip()


def visible_views(terminal, cache=None, runner=run_tmux):
    """Return terminal tabs displaying this pane, without changing tmux selection."""
    if terminal.get('remote'):
        return []
    socket, pane = terminal.get('tmuxSocket'), terminal.get('tmuxPane', '')
    if not socket or not re.fullmatch(r'%\d+', pane):
        return [{'windowId': terminal.get('windowId', ''),
                 'gnomeTerminal': terminal.get('gnomeTerminal')}]
    cache = {} if cache is None else cache
    if socket not in cache:
        views = {}
        try:
            clients = runner(socket, 'list-clients', '-F',
                             '#{client_pid}\t#{pane_id}\t#{pane_tty}')
            for client in clients.splitlines():
                pid, active, tty = client.split('\t')
                environment = process_environment(int(pid))
                views.setdefault((active, tty), []).append({
                    'windowId': environment.get('WINDOWID', ''),
                    'gnomeTerminal': gnome_target(environment)})
        except (OSError, ValueError, subprocess.SubprocessError):
            views = {}
        cache[socket] = views
    # The tty check also protects against reused pane IDs.
    return cache[socket].get((pane, terminal.get('tty')), [])


def focus_target(terminal, runner=run_tmux):
    if not alive(terminal.get("process", {})):
        raise ValueError("This agent has exited. Its terminal is no longer tracked.")
    if terminal.get("remote"):
        raise ValueError("This session is remote. Open its SSH terminal manually.")
    result = {"pids": [p["pid"] for p in terminal.get("ancestors", []) if alive(p)],
              "windowId": terminal.get("windowId", ""), "tmux": False,
              "gnomeTerminal": terminal.get('gnomeTerminal')}
    pane, socket = terminal.get("tmuxPane", ""), terminal.get("tmuxSocket", "")
    if not socket or not re.fullmatch(r"%\d+", pane):
        return result
    try:
        # Verify that this is still the same pane, not a reused tmux pane ID.
        details = runner(socket, "display-message", "-p", "-t", pane,
                         "#{pane_tty}\t#{session_id}\t#{window_id}").split("\t")
        if len(details) != 3 or details[0] != terminal.get("tty"):
            raise ValueError("The original tmux pane is no longer available.")
        _, session, window = details
        clients = runner(socket, "list-clients", "-F", "#{client_pid}\t#{client_tty}\t#{session_id}\t#{client_activity}")
        if not clients:
            raise ValueError("This tmux session is detached. Attach it in a terminal first.")
        runner(socket, "select-window", "-t", f"{session}:{window}")
        runner(socket, "select-pane", "-t", pane)
        choices = []
        for client in clients.splitlines():
            pid, tty, current, activity = client.split("\t")
            choices.append((current == session, int(activity or 0), int(pid), tty))
        _, _, pid, tty = max(choices)
        runner(socket, "switch-client", "-c", tty, "-t", session)
        pids = [p["pid"] for p in ancestors(pid)]
        # WINDOWID inherited when the server started may belong to an old client.
        result.update(pids=list(dict.fromkeys(pids)), windowId="", tmux=True,
                      gnomeTerminal=gnome_target(process_environment(pid)))
        return result
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError("Could not select the tmux pane. Is its server still running?") from error
