# Compatibility rules

tunhub runs on your computers, reads files that you write, and is reached from
your browser's bookmarks. These are the rules its development follows: what
must keep working across upgrades, and how a change to it is introduced.

They are maintainer guidelines, not yet a formal promise to users; that may
come once the design has been stable for a while. `tests/test_contract.py`
checks the interfaces listed here.

## Versions

tunhub uses `MAJOR.MINOR.PATCH` version numbers.

- A **patch** release (0.1.4 → 0.1.5) never breaks a stable interface. It may
  fix bugs, add features and config keys, and start deprecations.
- A **minor** release (0.1 → 0.2) may remove what was deprecated, as
  described under [Deprecation](#deprecation). While tunhub is at 0.x, that is
  the only way a stable interface goes away.
- From **1.0** on, removals need a major release.
- A fix for a security problem may tighten behaviour in any release, for
  example by rejecting a value that used to be accepted. The changelog lists
  it under **Security**.

## Stable interfaces

### Files and locations

- The config directory: `$XDG_CONFIG_HOME/tunhub` or `~/.config/tunhub`
  (Linux), `~/.config/tunhub` (macOS), `%APPDATA%\tunhub` (Windows).
  `TUNHUB_CONFIG_DIR` or `--config-dir` (before or after the command) chooses
  another one.
- In it:
  - `services.yaml`: the shared definition, the same on every machine.
  - `local.<machine>.yaml`: optional overrides for one machine, merged on top
    of `services.yaml`.
  - `known_macs`: see below.
- **The machine name** is `TUNHUB_MACHINE` if set, else the operating
  system's host name (on macOS the *LocalHostName*), lowercased, up to the
  first dot: `Laptop.lan` reads `local.laptop.yaml`. The dashboard shows it
  next to its title.
- **tunhub never modifies a file you write** (`services.yaml`,
  `local.<machine>.yaml`). Only `tunhub init` creates them, and
  `local.example.yaml` belongs to tunhub (`tunhub init --force` overwrites
  it). A later version may add files of its own to the directory, such as
  per-machine state, but never edits yours.
- Reserved for future use: the subdirectory `conf.d/`.

### Configuration

- Every key documented in the example `services.yaml`, with its meaning, its
  value format (`host:port`, `[v6]:port`, a bare port for `remote`) and the
  values it accepts.
- How the local file is merged: mappings key by key; every other value, lists
  such as `routes` included, replaces the one in `services.yaml`.
- `null` means "not set": the default applies.
- Defaults that decide addresses: a service's `hostname` (`<id>.localhost`),
  `hub.hostname` (`hub.localhost`) and `hub.port` (`1110`).
- Service ids: lowercase letters, digits and inner hyphens, at most 63
  characters (one DNS label). An explicit `hostname` is an IPv4 address or a
  valid DNS name.
- Reserved for future use: the top-level key `defaults:`.

**Unknown keys** are warnings (in the log and on the dashboard), not errors,
so an older tunhub can still read a config written for a newer one.

**Requiring a version.** When machines with different tunhub versions share
one config, a config can name the versions it is written for (a
[PEP 440](https://peps.python.org/pep-0440/#version-specifiers) range):

```yaml
requires_tunhub: ">=0.1.5"
```

A tunhub outside the range then stops with "please upgrade" instead of
misreading settings it does not know.

### Addresses

- The dashboard at `http://<hub.hostname>:<hub.port>/`, also served as
  `localhost`, `127.0.0.1` and `[::1]` on that port.
- `/open/<id>`: connect the service, then redirect to its address (a TCP
  service redirects to the dashboard). Paths and query strings after
  `/open/<id>` (`/open/<id>/...`, `/open/<id>?...`) are reserved for future
  use, such as deep links.
- A service's address, `http(s)://<hostname>:<local_port><path>`, which stays
  the same whichever route is in use.
- The dashboard and everything tunhub opens listen on loopback only;
  `hub.listen` accepts loopback addresses only.

### known_macs

- One `<ip> <mac>` entry per line; `#` starts a comment. Several lines for one
  IP are all trusted. MACs are written in lowercase, separated by colons.
- Reserved for future versions, and skipped by older ones: lines that start
  with `@` (such as `@revoked ...`) and extra fields after the MAC (such as
  `net=home`).
- tunhub only appends to this file. `tunhub forget <ip>` removes only the
  lines whose first field is that IP address, and keeps every line it does
  not understand.

### Command line

- `tunhub [run]`, `init`, `service install|uninstall|status`, `forget`, with
  their documented options, and `python -m tunhub`.
- Exit codes:

  | code | meaning |
  |---|---|
  | 0 | success; `service status`: installed |
  | 1 | the operation could not be done; `service status`: not installed |
  | 2 | invalid arguments or configuration |

- Output text is **not** stable; do not parse it.

### Start at login

- The names: systemd user unit `tunhub.service`, launchd label
  `io.github.liukdiihmieu.tunhub`, Windows scheduled task `TunHub`.
- An installed entry runs `<python> -m tunhub run --config-dir <dir>`, and
  that command line keeps working in later versions, so upgrading tunhub does
  not require reinstalling the entry. If these names ever change,
  `tunhub service uninstall` also removes the old ones.

### Behaviour

- Ordered routes; ssh runs through the system OpenSSH client and your
  `~/.ssh/config`.
- Two states, auto and on. Auto never starts ssh; Stop returns a service to
  auto; `auto_direct: false` turns auto off for a service.
- A direct route is used only if the target's next-hop MAC is in
  `known_macs` (recorded on first use). If no MAC can be found, the route is
  allowed. Loopback targets are not checked.

## Internal: may change in any release

- The HTTP API under `/api/` and the `X-Tunhub` header: they exist for the
  dashboard only. To script tunhub, use `/open/<id>` (it works with `curl`)
  or the command line, and open an issue if something is missing.
- `/static/` paths, the dashboard's HTML and CSS, and the wording of log
  lines, warnings and error messages.
- The exact ssh command line. tunhub sets `BatchMode=yes` and
  `ExitOnForwardFailure=yes`; changes to those two follow the deprecation
  rules.
- Timing: probe intervals, back-off, the network-change check.
- The cache directory (`TUNHUB_CACHE_DIR`; icons) and its layout.
- Everything in the login entry other than the names and command above.
- Python modules and functions: tunhub is an application, not a library.

## Deprecation

When a stable interface has to change:

1. A release announces it. The old form still works, and tunhub warns where
   you will see it (a config warning on the dashboard, or the terminal for
   the command line), naming the replacement and the version that will
   remove the old form.
2. The old form keeps working for at least one minor release **and** at
   least three months after the release that deprecated it.
3. It is then removed in a minor release (a major release after 1.0), listed
   under **Breaking** in the changelog with migration steps.

An old form that is cheap to keep is kept: configs are synced between
machines that are upgraded at different times.

The defaults of tuning values (timeouts, intervals, the default
`ssh_options`) may change in a minor release with a changelog note. To keep
the old behaviour, set the value explicitly.

## Platforms

- Python 3.10 and newer. A Python version is dropped only in a minor release,
  announced in the changelog beforehand, and only when it causes real
  problems (for example a dependency no longer supports it).
- Linux, macOS and Windows 10 or later, with their built-in OpenSSH client.
- Installed from PyPI with `uv tool`, `pipx`, or `pip` in a virtual
  environment.

## Reporting a break

If an upgrade breaks something this page lists as stable, it is a bug. Please
open an issue: https://github.com/LiukDiihMieu/tunhub/issues.
