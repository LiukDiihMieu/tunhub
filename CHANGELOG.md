# Changelog

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
