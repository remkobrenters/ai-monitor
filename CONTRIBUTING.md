# Contributing

Bug fixes, accessibility improvements, and compatibility results are welcome.
Keep changes focused and preserve the standalone, local-first architecture.

## Development

Requirements: Python 3.11+, Node.js 18+, `gjs`, `glib-compile-schemas`, and the
system GI packages used by the preferences window. No Python or npm package
installation is needed.

Run focused tests while editing, then `make check` before submitting a change.
`make dist` verifies the distributable source allowlist and emits its checksum.
Use staged installer tests rather than changing a real desktop for unit tests:

```sh
./install.sh --destdir /tmp/ai-monitor-stage
./uninstall.sh --destdir /tmp/ai-monitor-stage --purge
```

Follow nearby Python/JavaScript conventions. Prefer the standard library and
native GNOME APIs. Avoid new dependencies, single-use abstraction layers, broad
exception suppression, and configuration without a real use. Test user-visible
flows and trust boundaries; mock desktop/network/process boundaries rather than
copying an implementation into a test.

Keep prompts, credentials, private paths, and real session data out of fixtures
and diagnostics. Use `/projects/example` and synthetic tokens. Do not play sounds
or make real provider requests from automated tests. Add regression coverage for
security fixes. Changes to hooks must preserve CLI trust and passive behavior.

For desktop or hardware results, follow `docs/desktop-check.md` and include the
distribution, architecture, GNOME and CLI versions, Xorg/Wayland, terminal,
tmux version if relevant, monitor scale, and observed behavior. State what was
actually tested. Attach only redacted screenshots.

## Pull requests

Describe the problem, the resulting behavior, validation, and remaining limits.
The contributor retains their rights and submits source changes under the MIT
license. Third-party assets require their own provenance and compatible notices;
do not assume the source-code license also covers a recording or logo.
