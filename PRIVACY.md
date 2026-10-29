# Privacy

ai-monitor runs as the logged-in desktop user. It has no telemetry endpoint,
advertising SDK, cloud database, or ai-monitor account.

## What it reads

- Claude/Codex lifecycle events, limited to identifiers, timestamps, working
  directories, tool names, terminal identity, and subagent activity.
- Local CLI records to extract numeric token usage and native session-name
  metadata. Those files contain conversations; records may be parsed in memory,
  but message bodies are not copied into ai-monitor state, logs, or network calls.
- Same-user Linux process identities, working directories, terminal devices, and
  a small allowlist of environment variables. Full environments are not stored.
- GNOME Terminal screen IDs and active-tab state; tmux pane/client identities.
  It does not scrape terminal text or record keyboard input.
- Codex's exact-turn approval routing metadata, from a bounded portion of its
  local session record. Only the turn ID and reviewer enum are retained for that
  turn; instructions and conversation contents are not forwarded or stored.

Hook payloads do not forward prompts, tool arguments, agent answers, shell
commands, or background-task descriptions. Session names are taken from naming
metadata; conversation text is not used as a naming fallback.

## Local storage

The state directory defaults to `~/.local/state/ai-monitor`. It contains numeric
daily totals, cached allowance data, native session IDs/names and project paths,
process/terminal identities, subagent bookkeeping, hook delivery diagnostics,
and installer ownership records. Session metadata is personal data, even without
conversation text. Exit detection removes ended sessions; numeric history remains
until purged. The application does not encrypt these files at rest.

State files are written atomically with user-only file permissions. The service's
Unix socket is private and validates the connecting Linux user. This is a
same-user boundary, not protection against another application already running as
that user or an administrator.

The installer records only its hook ownership and restoration flags; new records
do not back up unrelated CLI settings or credentials. Updating an older install
migrates its previous full-settings record to this minimal format.

## Network and credentials

Claude allowance requests use its existing OAuth access token over HTTPS to
`api.anthropic.com`. Redirects are rejected, response sizes are bounded, and login
tokens are never stored in ai-monitor's state or refreshed by it. Codex allowance
requests use the installed Codex CLI's `app-server` and existing login. The CLI
controls its own network transport and authentication.

Lookups run at most once per provider every ten minutes while the panel reports
an unlocked desktop, with rate-limit backoff. `ai-monitor serve --no-network`
disables allowance lookups when running the service manually. It does not change
the installed systemd unit. Local records may still supply Codex windows.

No prompts, source code, token history, project paths, session names, or analytics
are uploaded by ai-monitor. Opening the repository link launches the default web
browser; its normal browser privacy settings apply. Bundled sound playback never
contacts its source website. Custom sounds are local files.

## Sharing and deletion

`ai-monitor status`, logs, and screenshots may reveal session titles, project
paths, allowance levels, or usage. Redact them before posting reports. Never
include CLI credentials, complete settings, transcripts, or the state directory.

`ai-monitor uninstall` removes the application and its hooks while retaining
history/preferences. `ai-monitor uninstall --purge` also deletes ai-monitor's
state and resets its GSettings preferences. Neither deletes Claude/Codex's own
history or login. Deletion is ordinary filesystem deletion, not secure erasure.
