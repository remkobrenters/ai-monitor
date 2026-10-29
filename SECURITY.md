# Security

## Supported versions

Security fixes target the latest release. Version 1.0.x is the current release
line. Dependencies are supplied by the Linux distribution and installed CLIs;
keep those up to date as well.

## Reporting a vulnerability

Use [GitHub private vulnerability reporting](https://github.com/remkobrenters/ai-monitor/security/advisories/new)
when available. If private reporting is disabled, open a minimal issue asking for
a private reporting channel without exploit details or sensitive data. There is
no guaranteed response-time commitment.

Provide the affected version, an isolated reproduction, the attacker access
required, impact, and expected behavior. Use synthetic sessions and fixture
credentials. Do not upload tokens, actual transcripts, personal paths, complete
settings, or status dumps without redaction.

## Trust boundaries

- The service is a systemd **user** service with `NoNewPrivileges` and a private
  runtime directory; it is not a root service or a network listener.
- Local JSON messages are bounded. The socket checks the Linux peer UID.
- Hooks are passive observers. They never approve requests, answer questions,
  change prompts, or bypass CLI trust reviews. Monitor failures do not block work.
- Terminal navigation uses explicit D-Bus/tmux/window APIs, without simulated
  keyboard input. Remote terminal forwarding is not implemented.
- Claude access tokens are sent only to the fixed HTTPS usage endpoint;
  redirects are rejected. Provider response/error contents are not trusted for
  exposing arbitrary text in diagnostics.
- Install/uninstall use exact owned hook commands and reject symlinked managed
  destinations and control characters in service paths. Updates replace installed
  code directories; local history lives separately.

Other processes running under the same account can read that account's files,
change state, or call its private socket. ai-monitor does not sandbox the user's
CLI or protect against compromise of the desktop account.

Review and regression checks improve security but do not certify the application
as vulnerability-free. See [Privacy](PRIVACY.md) for retained metadata and
[desktop checks](docs/desktop-check.md) for unverified integration behavior.
