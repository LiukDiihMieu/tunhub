# Changelog

## Unreleased

### Changed

- A configuration error no longer stops tunhub: the dashboard starts anyway
  and shows the error in a red box, with a *Reload config* button, until the
  file is fixed (bookmarks show it too). tunhub listens where the `hub:`
  section says, or, if the files cannot be read at all, where the last
  working configuration did. `tunhub run` exits with code 2 only when
  neither is known. (Being more tolerant is not a breaking change; this is
  now written in `docs/COMPATIBILITY.md`.)
- The config error and warning boxes have their own *Reload config* button.
- The top bar keeps only *Disconnect SSH*; *Reload config* moved into a new
  **⋯** menu, which also offers *Copy config path*, *Events* and *Restore
  card order*.

### Added

- **Events** panel (⋯ menu): the recent events of tunhub as a whole, such as
  network changes, reloads, config warnings and errors, and every service's
  log lines, with the network tunhub currently sees (gateway and subnet).
- **Card order**: drag a card onto another one to move it. The order is kept
  per machine (not in your YAML); *Restore card order* in the ⋯ menu goes
  back to the order of `services.yaml`. New services appear at the end.
- A per-machine state directory for things tunhub remembers by itself:
  `$XDG_STATE_HOME/tunhub` (Linux), `~/Library/Application Support/tunhub`
  (macOS), `%LOCALAPPDATA%\tunhub` (Windows); `TUNHUB_STATE_DIR` overrides
  it.

## 0.2.0 — 2026-11-10

Settling the parts that should stay stable in the long term (configuration,
addresses, `known_macs`, the command line). A few changes need action when
you upgrade; they are listed first.

### Breaking

- **Per-machine overrides moved from `local.yaml` to `local.<machine>.yaml`.**
  Each machine reads only its own file, so the whole config directory can be
  synced. `local.yaml` is no longer read: rename it. The machine name is shown
  next to the dashboard's title (the host name, lowercased, up to the first
  dot; `TUNHUB_MACHINE` or `--machine` sets another, and `tunhub service
  install` records it).
- **The default dashboard port is now 1110** (was 8765). Only configs without
  `hub.port` are affected: set `hub: {port: 8765}` to keep the old address
  and your bookmarks, or update the bookmarks.
- **Stricter configuration.** These used to be accepted and are now errors:
  - Service ids that are not a DNS label (letters, digits, inner hyphens, at
    most 63 characters), and host names that are not valid DNS names or IPv4
    addresses.
  - A service host name equal to the dashboard's (e.g. a service called
    `hub`), or two services with the same host name (IP addresses and
    `localhost` may be shared).
  - A route with both `direct` and `ssh` (usually a missing `-`).
  - `true`/`false`, lists or mappings for text values (`ssh: on` used to
    become the alias `True`).
- `direct` routes are IPv4 only: an IPv6 address is an error, and a host name
  is resolved to IPv4 addresses. On macOS and Windows, `known_macs` could not
  check IPv6 targets and silently let them through. Reach IPv6 targets
  through an `ssh` route (`remote: "[2001:db8::1]:5000"`).
- `tunhub forget` with something other than an IP address is an error (exit
  code 2); it used to remove every line not starting with an IP address.

### Security

These also used to be accepted and are now errors:

- `hub.listen` other than a loopback address such as `127.0.0.1` or `::1`:
  the dashboard has no login, so it must not be reachable from other devices.
- An ssh alias starting with `-`, which ssh would read as an option (for
  example `-oProxyCommand=...`).

### Added

- Config warnings, in the log and in a yellow box on the dashboard, for
  problems that do not stop tunhub: unknown keys (with "did you mean"), a
  direct route to 127.0.0.1 in the shared `services.yaml`, host names outside
  `.localhost` that do not resolve to this machine, and IP addresses of other
  machines as host names (the browser would bypass tunhub).
- `requires_tunhub: ">=X.Y"` in a config names the tunhub versions it is
  written for; an older tunhub stops with "please upgrade" (0.1.0 ignores
  the key).
- `null` for an optional key means "not set" (the default), also in
  `tunnel.ssh_options`, where it undoes an option set in `services.yaml`.
- `--config-dir` is also accepted before the command (`tunhub --config-dir X`).
- Python 3.14.

### Changed

- `tunhub service install` always records the config directory it used (as
  an absolute path; symlinks are kept, not resolved), so
  the login service no longer depends on the shell's environment (e.g.
  `XDG_CONFIG_HOME`). Run it again after upgrading if you use such a setting.
- `tunhub service status` exits with 1 when the service is not installed.
- Exit codes: 0 success, 1 the operation could not be done, 2 invalid
  arguments or configuration.

## 0.1.0 — 2026-11-10

First public release.

### Connecting

- Ordered routes per service: a direct TCP relay or `ssh -L`, always on the
  same fixed local port, so bookmarks, cookies and saved passwords keep working
  whichever route is used. Web services get their own `<id>.localhost` host
  name.
- Bookmarkable `/open/<id>` URLs: open one like any website and tunhub
  connects first (starting ssh if needed), waits for an HTTP answer, then
  forwards you.
- Two states per service. **Auto** (default): connected directly while a
  direct route is reachable, never through ssh. **On**: opened while not up,
  kept up through ssh, retried with back-off until you press Stop, which
  returns it to auto. A connection that lands on a direct route settles in
  auto, so a laptop that leaves home does not keep ssh tunnels it no longer
  needs.
- Non-web services (RDP, VNC, databases, ...) as tiles that connect and copy
  their `127.0.0.1:<port>` address.
- *Disconnect SSH* stops every ssh connection in one click.

### Not connecting to the wrong machine

- `known_macs`, like ssh's `known_hosts`: the first direct connection records
  the next-hop MAC of its target; later, a direct route is used only if it
  still matches (otherwise it falls back to ssh and the tile shows ⚠).
  `tunhub forget <ip>` removes an entry.
- A network watcher (gateway IP and MAC, local subnet) re-verifies running
  direct routes when the network changes or cannot be determined; routes that
  still reach the same machine are not interrupted.

### Dashboard

- Tiles with icons discovered from each web service through its own local
  port, a detail drawer (address, bookmark, route choice, live log) and
  light/dark themes. Installable as an app in Chromium-based browsers.
- A first start without a configuration shows how to add the first service.
- A clear warning when tunhub itself is not reachable, and a red dot on the
  tab icon when something needs attention.

### Configuration and setup

- Shared `services.yaml` plus a per-machine `local.yaml`; *Reload config*
  keeps unchanged services connected. Every value is validated, with errors
  that name the setting.
- `tunhub init` writes a commented example; `tunhub service
  install|uninstall|status` starts tunhub at login (systemd, launchd, Windows
  Task Scheduler).
- Windows: ssh processes end together with tunhub, even when it is killed.

### Security

- The dashboard and every forward listen on 127.0.0.1 only.
- Requests with a foreign `Host` are rejected (DNS rebinding); state-changing
  calls need a custom header and a same-origin `Origin`; other web pages cannot
  open tunnels through `/open/<id>` or frame the dashboard.
