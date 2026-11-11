<h1>
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/LiukDiihMieu/tunhub/main/docs/images/logo-dark.svg">
    <img src="https://raw.githubusercontent.com/LiukDiihMieu/tunhub/main/docs/images/logo-light.svg" alt="tunhub" height="64">
  </picture>
</h1>

**Open the web UIs on your private network like any public website. tunhub sets up the SSH forwarding for you.**

[![PyPI](https://img.shields.io/pypi/v/tunhub)](https://pypi.org/project/tunhub/)
[![Python versions](https://img.shields.io/pypi/pyversions/tunhub)](https://pypi.org/project/tunhub/)
![Platforms](https://img.shields.io/badge/platform-Linux%20%7C%20macOS%20%7C%20Windows-lightgrey)
[![License: GPL v3+](https://img.shields.io/badge/license-GPL--3.0--or--later-green)](https://github.com/LiukDiihMieu/tunhub/blob/main/LICENSE)

<p align="center">
  <img src="https://raw.githubusercontent.com/LiukDiihMieu/tunhub/main/docs/images/demo.gif" alt="Opening AdGuard from a browser favourite: tunhub starts the SSH tunnel by itself (the tile turns from grey to amber to purple) and the AdGuard page opens" width="760">
  <br><sub>A plain browser favourite opens AdGuard; tunhub starts the SSH tunnel on the way.</sub>
</p>

You self-host services with a web UI: Jupyter on a lab workstation, the admin
page of your NAS, AdGuard, Grafana, ... For security, they stay off the public
internet. So every time you need one, you first start an `ssh -L` forward, then
recall which local port it was, then type the address. Again and again, on every
computer you use.

tunhub takes those steps away. It runs quietly on your own computer: click a
tile on its dashboard, or simply open a bookmark, and tunhub picks a route,
opens the SSH tunnel if one is needed, waits until the service answers, and
takes you there. When you are on the same LAN, it skips SSH and connects directly.

- **One click from the hub.** Click a tile and the service opens in a new tab,
  with the tunnel already up.
- **Bookmarks that just work.** Save `http://hub.localhost:1110/open/nas` in your
  favourites bar and use it like any public website. If the tunnel is down,
  tunhub opens it on the way; you never think about it.
- **Direct when it can, SSH when it must.** On the same LAN, tunhub connects
  directly (cyan dot). Everywhere else, it goes through SSH (purple dot).
- **Addresses that never change.** The NAS is always `http://nas.localhost:15000`,
  whatever the route, so saved passwords and cookies keep working.
- **Not just web pages.** RDP, VNC, databases and other TCP services get an
  on/off tile that copies their address for your client.
- **Lightweight.** One Python process using about 50 MB of memory and
  practically no CPU when idle, plus about 4 MB for each active ssh tunnel.

<p align="center">
  <img src="https://raw.githubusercontent.com/LiukDiihMieu/tunhub/main/docs/images/dashboard.png" alt="The tunhub dashboard: one tile per service, with a status dot showing whether it is connected directly or through SSH" width="820">
</p>

## Get started

You need an OpenSSH client (built into Linux, macOS and Windows 10+) and
Python 3.10+. Install tunhub with any of:

```bash
uv tool install tunhub          # uv: its own environment; fetches Python if needed
pipx install tunhub             # pipx: its own environment
pip install tunhub              # pip: inside a virtual or conda environment
```

With pip, use a virtual or conda environment (many Linux distributions and
Homebrew refuse a system-wide `pip install`), and keep that environment:
`tunhub service install` starts tunhub with its Python. If the `tunhub` command
is not on your PATH, `python -m tunhub` does the same.

Then:

```bash
tunhub init                     # writes a commented example config
$EDITOR ~/.config/tunhub/services.yaml
tunhub                          # then open http://hub.localhost:1110
tunhub service install          # optional: start automatically at login
```

Started without a config, the dashboard shows where the file goes and how to
add your first service.

In Chromium-based browsers (Chrome, Edge, Brave) you can also install the
dashboard as an app from the address bar. It then opens in its own window with
its own dock or taskbar icon.

A service is a local port plus a list of routes, tried in order:

```yaml
services:
  nas:
    local_port: 15000
    routes:
      - direct: 192.168.1.10:5000   # tried first, when you are at home
      - ssh: nas                    # Host alias from ~/.ssh/config
        remote: 127.0.0.1:5000      # as seen from the SSH server
```

SSH routes run non-interactively, so they need key authentication (or a loaded
ssh-agent) and a host that is already in `known_hosts`. Run `ssh <alias>` once
by hand for each host before you add it.

## How it works

```mermaid
flowchart LR
    B["Browser<br/>nas.localhost:15000"] --> P["127.0.0.1:15000<br/>(tunhub)"]
    P -- "on the LAN: direct" --> N["NAS<br/>192.168.1.10:5000"]
    P -- "elsewhere: ssh -L" --> S["SSH host"] --> N
```

Whichever route wins, the service is served at the same local address:

| route  | what listens on 127.0.0.1:15000                  |
|--------|--------------------------------------------------|
| direct | a TCP relay inside tunhub (like `socat`)         |
| ssh    | `ssh -N -L 127.0.0.1:15000:127.0.0.1:5000 nas`   |

Each web service gets its own host name, `http://<id>.localhost:<local_port>`.
Browsers share cookies between ports, but not between host names, so this keeps
logins and password-manager entries apart. Chromium-based browsers and Firefox
resolve `*.localhost` to 127.0.0.1 without any DNS setup.

### Address or bookmark?

Every web service has two URLs, both shown in its ⋯ panel:

| | example | what it is for |
|---|---|---|
| **Bookmark** | `http://hub.localhost:1110/open/nas` | Save this one in your browser. Opening it connects the service first (starting ssh if needed), then forwards you to the address. |
| **Address** | `http://nas.localhost:15000` | Where the service itself lives. Your password manager and cookies are tied to it, and it never changes with the route. It only loads while the service is connected. |

So you bookmark the hub link, and your password manager remembers the login
under the address, without any extra setup.

### Connection behaviour

A service is either in **auto** (the default) or **on**:

- **Auto**: every 15 s, tunhub probes the direct routes of idle services and
  brings a service up as soon as one is reachable (cyan, "Direct").
  This only opens the local port; nothing is kept open to the target until
  you use it. Auto never starts ssh: when the direct route is lost, the
  service simply goes back to off.
- **On**: opening a service that is *not* up (tile click, bookmark, Start,
  `autostart: true`) tries the routes in order, including ssh. If it comes up
  on a direct route, it is simply in auto; if it needs ssh (purple, "SSH"),
  it stays on: tunhub keeps it up, and if ssh dies it tries all routes again
  from the first, backing off from 2 s to 60 s while every route fails. A
  working ssh connection is kept even if a direct route becomes reachable,
  so live sessions are not cut; when it drops (e.g. the laptop changed
  networks), the reconnect prefers the direct route and the service is back
  in auto. Opening a service that is already up just opens it, so a laptop
  that leaves home does not start ssh tunnels it no longer needs.
- **Stop** (shown only for a service that is on) ends the connection and
  returns the service to auto; at home it is back on the direct route
  within a second. A service with `auto_direct: false` has no auto mode, so
  any connection to it is on until you stop it. There is no "stay off": set `auto_direct: false` for a
  service that should never connect by itself. **Disconnect SSH** in the
  top bar does the same for every service that uses or is trying ssh.
- After a network change, ssh notices its dead connection within about 45 s
  (keep-alive). To switch immediately, click Stop and then the tile.

The **⋯** button on a tile shows its address, bookmark URL, route choice and live log:

<p align="center">
  <img src="https://raw.githubusercontent.com/LiukDiihMieu/tunhub/main/docs/images/drawer.png" alt="Service details: address, bookmark URL, route selector and the active route" width="420">
</p>

## Configuration

`tunhub init` creates the config directory with a commented example:

| OS      | config directory                                |
|---------|-------------------------------------------------|
| Linux   | `$XDG_CONFIG_HOME/tunhub` or `~/.config/tunhub` |
| macOS   | `~/.config/tunhub`                              |
| Windows | `%APPDATA%\tunhub`                              |

To use another directory, set `TUNHUB_CONFIG_DIR` or pass `--config-dir`.

- **`services.yaml`** is the shared definition: the same file on every
  machine. Sync the whole directory between your machines (a dotfiles repo,
  a synced folder, ...).
- **`local.<machine>.yaml`** holds optional overrides for one machine and is
  merged on top of `services.yaml`: mappings key by key, lists such as
  `routes` as a whole. `<machine>` is the name shown next to the dashboard's
  title (the host name, lowercased, up to the first dot; `TUNHUB_MACHINE` or
  `--machine` sets another). Each machine reads only its own file. See
  `local.example.yaml`.

Write the routes as "how to reach this service from my other computers":
`direct` for networks where you can reach it directly (one `direct` per
network if its address differs), `ssh` for everywhere else. Keep machine
differences out of `services.yaml`:

- If machines reach a server differently over ssh (a jump host from outside,
  a plain connection inside), give the same `Host` alias a different
  definition in each machine's `~/.ssh/config`.
- A port that is taken on one machine, or a service that one machine should
  not show (`enabled: false`), goes into that machine's local file.

Click **Reload config** in the dashboard after editing. Services whose
definition did not change keep their connections. Changes to the `hub:`
section need a restart. Problems that do not stop tunhub, such as a misspelt
key, are shown in a yellow box on the dashboard until you fix them.

<details>
<summary>Running tunhub on the machine that hosts a service</summary>

Nothing special is needed. If the service listens on the LAN, its `direct`
route works on that machine too. If it only listens on 127.0.0.1, the `ssh`
route still works (ssh to itself). To skip ssh there, add a
`direct: 127.0.0.1:<port>` route in **that machine's** local file, never in
`services.yaml`: 127.0.0.1 is a different machine on every computer. Its
`local_port` must differ from the service's own port.
</details>

Every key is explained in the comments of the example files that `tunhub init`
writes: [`services.yaml`](https://github.com/LiukDiihMieu/tunhub/blob/main/tunhub/templates/services.yaml) and
[`local.example.yaml`](https://github.com/LiukDiihMieu/tunhub/blob/main/tunhub/templates/local.example.yaml).

## Start at login

```bash
tunhub service install     # also: uninstall, status
```

| OS      | mechanism                          | logs |
|---------|------------------------------------|------|
| Linux   | systemd user unit `tunhub.service` | `journalctl --user -u tunhub -f` |
| macOS   | LaunchAgent                        | `~/Library/Logs/tunhub.log` |
| Windows | Task Scheduler (`pythonw`, no console window) | `%LOCALAPPDATA%\tunhub\tunhub.log` |

The service runs the same Python interpreter that installed it, with the
config directory that `tunhub service install` used, and the machine name if
you chose one (`TUNHUB_MACHINE` or `--machine`). Both are written into the
service, so it does not depend on your shell's environment. After moving the
config directory, run `tunhub service install` again.

This starts *tunhub* at login. A service's `autostart: true` is different: it
connects that service whenever tunhub starts.
On Linux, the systemd user session must see your ssh-agent (`SSH_AUTH_SOCK`);
GNOME's agent normally takes care of that.

On Windows, stop tunhub before upgrading it, because Windows cannot replace a
program that is running (`uv` then fails with "Access is denied"):

```powershell
Stop-ScheduledTask -TaskName TunHub
uv tool upgrade tunhub
Start-ScheduledTask -TaskName TunHub
```

## Security

- The dashboard and all forwards listen on this machine only (127.0.0.1;
  `hub.listen` accepts only loopback addresses such as `::1`).
- Requests whose `Host` is not the dashboard's own host name are rejected, which
  blocks DNS rebinding.
- State-changing API calls need a custom header and a same-origin `Origin`, so
  other web pages cannot trigger them.
- Other web pages cannot open a tunnel through `/open/<id>` either (for example
  with a hidden `<img>`): tunhub only accepts bookmarks, typed URLs, the
  dashboard itself and links that you click.
- The dashboard refuses to be shown inside another site's frame (clickjacking).
- Only services defined in your config can be opened. The web UI never runs
  arbitrary commands.
- Service icons are fetched only through the service's own local port; tunhub
  never contacts other sites. They are served with a sandboxing CSP and shown
  only as `<img>`.

### Direct routes on other networks: `known_macs`

Private addresses are reused everywhere. In a café whose network also uses
`192.168.1.x`, another device might answer on your NAS's address, and the
browser would send it your NAS cookies. tunhub avoids such accidental
misconnections the way ssh handles host keys with `known_hosts`:

- The first time a direct route connects, tunhub records the MAC address of
  the next hop toward the target (the target itself on the same subnet, else
  the router) in `known_macs`, next to `services.yaml`:
  `Permanently added 192.168.1.10 (aa:bb:cc:dd:ee:01) to known_macs`.
- Later, a direct route is used only if that MAC matches. Otherwise it is
  skipped (falling back to ssh, if configured) and the tile shows ⚠.
- When the network changes, or cannot be determined, running direct routes
  are checked again; a route that still reaches the same machine stays up.
- If a MAC changes for a good reason (new network card, another router),
  run `tunhub forget <ip>` or edit `known_macs`; several lines for one IP are
  all trusted. The file can be synced together with your config.

This prevents accidents, not deliberate attacks: someone on the same network
can fake a MAC. Give direct targets a fixed address (a static IP or a DHCP
reservation): a running direct route is re-checked when the network changes,
not when an address is handed to another machine on the same network.

**Known limitation: direct routes over a VPN.** Through a VPN there is no
next-hop MAC to see, and tunhub cannot tell your VPN from another one that
routes the same private range elsewhere. So an address recorded on your LAN
is not used directly while you are on a VPN: the tile shows ⚠ and the next
route, usually ssh, is used instead. An address never recorded (seen only
through a VPN) is used without the check.

## How does it compare?

|                                   | opens on click | picks direct or SSH | nothing on servers | non-web services |
|-----------------------------------|:---:|:---:|:---:|:---:|
| **tunhub**                        | ✅ | ✅ | ✅ | ✅ |
| SSH tunnel managers (GUI / web)   | –  | –  | ✅ | ✅ |
| Homelab dashboards (Homer, Homepage, Dashy) | ✅ | – | ✅ | – |
| Mesh VPNs (Tailscale, ZeroTier, WireGuard) | – | ✅ | – | ✅ |

- **SSH tunnel managers** handle individual `-L` forwards. You still map ports
  to services in your head, and nothing connects when you open a bookmark.
- **Homelab dashboards** are link pages: they can't reach a service that needs a tunnel.
- **Mesh VPNs** are great when you can install an agent on every server. tunhub
  is for the places where you can't or don't want to: university or company
  machines reachable only over SSH, jump hosts, or a NAS you would rather not
  run a VPN on.

## What tunhub is not

- **Not a VPN** and nothing to install on your servers: it uses the SSH access
  you already have. For a mesh network, use Tailscale, ZeroTier or WireGuard.
- **Not a way to publish services**: everything listens on 127.0.0.1, and the
  dashboard is for you alone, with no login and no access from other devices.
  For public URLs, see ngrok or Tailscale Funnel.
- **Not an SSH client**: it runs your system's OpenSSH with your
  `~/.ssh/config`, so jump hosts, certificates, agents and hardware keys work
  as they do in your terminal.
- **Not an HTTP proxy**: it forwards bytes, so any protocol works, but it
  does not rewrite headers or terminate TLS.
- **Not a monitoring or discovery tool**: it opens the services you list. For
  status pages and alerts, see Uptime Kuma or Homepage.
- **Not a config editor**: you write the YAML; tunhub never changes it.

## Development

```bash
git clone https://github.com/LiukDiihMieu/tunhub && cd tunhub
uv sync --extra dev
uv run pytest
uv run tunhub --config-dir ./my-test-config
```

Changes follow the compatibility rules in
[`docs/COMPATIBILITY.md`](https://github.com/LiukDiihMieu/tunhub/blob/main/docs/COMPATIBILITY.md):
config files, bookmarks, addresses, `known_macs` and the command line keep
working across upgrades, while the HTTP API under `/api/` is internal to the
dashboard.

The tests use `tests/fake_ssh.py` in place of ssh to exercise the SSH routes,
including reconnecting after the ssh process is killed.

Icons, logos and the social preview image are generated by
`tools/make_icons.py`; see its docstring to regenerate them.

## License

[GPL-3.0-or-later](https://github.com/LiukDiihMieu/tunhub/blob/main/LICENSE). You may use, modify and redistribute tunhub, but any
distributed modified version or derivative work must also be released under the GPL.
