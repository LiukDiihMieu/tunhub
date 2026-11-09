# tunhub

**One local dashboard for your SSH-tunnelled and LAN services. Bookmark once, open from anywhere.**

<!-- TODO: screenshot of the dashboard -->

You have web UIs and desktops spread over a NAS, a lab workstation and a cloud
VM. Some are reachable directly when you are on the same LAN, and others only through
SSH. Without tunhub, you type an `ssh -L` command, remember which local port belongs to which
service, and then open the browser.

tunhub runs on each of your computers (Linux, macOS, Windows) and turns that
into one click:

- **A tile per service.** Click the icon and the service opens. tunhub picks the
  route, starts the tunnel if needed, waits until the service answers, and
  redirects you.
- **Bookmarkable.** `http://hub.localhost:8765/open/nas` works from the browser's
  favourites bar on every machine, whether you are at home, in the lab or on the road.
- **Direct first, SSH as fallback.** Each service has an ordered list of routes.
  While a direct route is reachable, the service connects by itself (blue dot);
  elsewhere it goes through SSH (green dot).
- **Stable addresses.** A service always lives at the same `http://nas.localhost:15000`,
  whichever route is used, so saved passwords, cookies and history keep working.
- **Not only web UIs.** RDP, VNC, databases and other TCP services get an on/off tile
  that copies `127.0.0.1:<port>` for your client.
- **Nothing on the servers.** tunhub uses your existing OpenSSH client and
  `~/.ssh/config`. It needs no agent, VPN, root rights or Docker.

## Install

Requires Python 3.10+ and an OpenSSH client (built into Linux, macOS and Windows 10+).

```bash
uv tool install tunhub          # or: pipx install tunhub
```

Until the first PyPI release, install from GitHub instead:
`uv tool install git+https://github.com/LiukDiihMieu/tunhub`.

## Quick start

```bash
tunhub init                     # writes an example services.yaml
$EDITOR ~/.config/tunhub/services.yaml
tunhub                          # start; open http://hub.localhost:8765
tunhub service install          # start automatically at login
```

SSH routes run with `BatchMode=yes`: key authentication (or a loaded ssh-agent)
is required, and every host must already be in `known_hosts`. Run `ssh <alias>`
once by hand for each host before using it in tunhub.

## How it works

```yaml
services:
  nas:
    local_port: 15000
    routes:
      - direct: 192.168.1.10:5000   # tried first (sub-second TCP probe)
      - ssh: nas                    # Host alias from ~/.ssh/config
        remote: 127.0.0.1:5000      # as seen from the SSH server
```

Whichever route wins, the service is served at the same local address:

| route  | what listens on 127.0.0.1:15000                    |
|--------|----------------------------------------------------|
| direct | an in-process TCP relay (byte-level, like `socat`) |
| ssh    | `ssh -N -L 127.0.0.1:15000:127.0.0.1:5000 nas`     |

Web services open as `http://<id>.localhost:<local_port>`. Giving each service its
own host name keeps cookies and password-manager entries apart, because cookies
are not isolated by port. Chromium-based browsers and Firefox resolve
`*.localhost` to 127.0.0.1 without any DNS setup.

### Connection behaviour

- **Manual connections** (tile click, bookmark, Start, `autostart: true`) try the
  routes in order and keep the service up. If ssh dies or a direct target
  disappears, tunhub tries all routes again from the first; while every route
  fails, retries back off from 2 s to 60 s.
- **Auto-direct** (on by default): every 15 s, tunhub probes the direct routes of
  idle services and brings a service up as soon as one is reachable. Auto
  connections only ever use direct routes and never start ssh. When the direct
  route is lost, an auto connection simply goes back to off.
- **Stop** on a tile pauses auto-direct for that service until you start it
  again. **Disconnect SSH** in the top bar stops every service that uses or is
  trying ssh, keeps direct connections up and does not pause auto-direct.
- After a network change, ssh notices its dead connection within about 45 s
  (keep-alive). To switch immediately, click Stop and then the tile.

## Configuration

`tunhub init` creates the config directory with a commented example:

| OS      | config directory                              |
|---------|-----------------------------------------------|
| Linux   | `$XDG_CONFIG_HOME/tunhub` or `~/.config/tunhub` |
| macOS   | `~/.config/tunhub`                            |
| Windows | `%APPDATA%\tunhub`                            |

To use another directory, set `TUNHUB_CONFIG_DIR` or pass `--config-dir`.

- **`services.yaml`** is the shared definition. Keep it in a dotfiles repo to
  sync it between your machines.
- **`local.yaml`** holds optional per-machine overrides and is merged on top of
  `services.yaml`: mappings key by key, lists such as `routes` as a whole. Use it when a
  machine *is* one of the servers (reach its services via `127.0.0.1`, hide its
  own remote desktop with `enabled: false`), or when a port is taken.
  See `local.example.yaml`.

Click **Reload config** in the dashboard after editing. Services whose definition did not
change keep their connections. Changes to the `hub:` section need a restart.

Per-service keys:

| key            | default                  | meaning |
|----------------|--------------------------|---------|
| `local_port`   | required                 | fixed port on 127.0.0.1 |
| `routes`       | required                 | ordered list of `direct: host:port` or `ssh: alias` + `remote: host:port` |
| `name`         | id                       | display name |
| `type`         | `http`                   | `http`, `https` or `tcp` |
| `hostname`     | `<id>.localhost`         | host name used in the browser |
| `path`         | `/`                      | path to open |
| `autostart`    | `false`                  | connect when tunhub starts |
| `auto_direct`  | `tunnel.auto_direct`     | connect automatically while a direct route is reachable |
| `health_check` | `true` for web           | wait for an HTTP answer before redirecting |
| `icon`         | discovered               | image file, relative to the config directory |
| `description`  |                          | shown in the dashboard |
| `enabled`      | `true`                   | `false` hides the service |

The `hub:` and `tunnel:` sections (port, timeouts, extra `ssh -o` options, ...)
are documented in the generated `services.yaml`.

## Dashboard

- **Click a tile**: a web service connects if needed and opens in a new tab,
  the same as its bookmark URL. A TCP service connects and copies its address.
- **Status dot**: blue = direct, green = ssh, amber = connecting,
  red = failing (hover for the error), grey = off.
- **⋯** opens a drawer with the address, the bookmark URL, route pinning,
  Start/Stop, *Refresh icon* and the live log.

**Icons** are discovered the first time a web service connects. tunhub fetches
them through the service's own local port and never contacts other sites.
It looks for a web-app manifest, then `apple-touch-icon`, then `<link rel=icon>`,
then `/favicon.ico`. Icons are cached in the OS cache directory, e.g.
`~/.cache/tunhub/icons`. A service without an icon shows coloured initials, and
`icon:` sets your own image.

## Start at login

```bash
tunhub service install     # also: uninstall, status
```

| OS      | mechanism                          | logs |
|---------|------------------------------------|------|
| Linux   | systemd user unit `tunhub.service` | `journalctl --user -u tunhub -f` |
| macOS   | LaunchAgent                        | `~/Library/Logs/tunhub.log` |
| Windows | Task Scheduler (`pythonw`, no console window) | `%LOCALAPPDATA%\tunhub\tunhub.log` |

The service runs the same Python interpreter that installed it. If you pass
`--config-dir` (or have `TUNHUB_CONFIG_DIR` set), it is written into the service.
On Linux, the systemd user session must see your ssh-agent (`SSH_AUTH_SOCK`);
GNOME's agent normally takes care of that.

## Service-specific notes

Some apps validate the `Host` header and must accept `<id>.localhost`:

- **qBittorrent** checks that the port in the `Host` header equals its own port.
  Keep `local_port` equal to the WebUI port, or turn off *Options → Web UI →
  Enable Host header validation*. If you keep the check, add
  `qbittorrent.localhost` to its domain list.
- **Jupyter**: on `403 Blocked host`, add
  `c.ServerApp.local_hostnames = ["localhost", "jupyter.localhost"]` to
  `jupyter_server_config.py`.
- If an app cannot be convinced, set `hostname: 127.0.0.1` for that service.

## Security

- The dashboard and all forwards listen on 127.0.0.1 only.
- Requests whose `Host` is not the dashboard's own host name are rejected, which
  blocks DNS rebinding.
- State-changing API calls need a custom header and a same-origin `Origin`, so
  other web pages cannot trigger them.
- Only services defined in your config can be opened. The web UI never runs
  arbitrary commands.
- Discovered icons are served with a sandboxing CSP and shown only as `<img>`.

## How does this compare?

- **SSH tunnel managers** (GUI or web) manage individual `-L` forwards. You still
  map ports to services in your head, and nothing connects when you open a bookmark.
- **Homelab dashboards** (Homer, Homepage, Dashy, ...) are link pages: they can't
  reach a service that needs a tunnel.
- **Mesh VPNs** (Tailscale, ZeroTier, WireGuard) are great when you can install an
  agent on every server. tunhub is for the places where you can't or don't want
  to: university or company machines reachable only over SSH, jump hosts, or a
  NAS you would rather not run a VPN on.

## Development

```bash
git clone https://github.com/LiukDiihMieu/tunhub && cd tunhub
uv sync --extra dev
uv run pytest
uv run tunhub --config-dir ./my-test-config
```

The tests use `tests/fake_ssh.py` in place of ssh to exercise the SSH routes,
including reconnecting after the ssh process is killed.

## License

MIT
