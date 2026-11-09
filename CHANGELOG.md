# Changelog

## 0.1.0 — unreleased

First public release.

- Dashboard of service tiles with icons discovered from each web service
- Ordered routes per service: direct TCP relay or `ssh -L`, with fixed local
  ports and `<id>.localhost` host names
- Bookmarkable `/open/<id>` URLs that connect on demand, with HTTP health check
- Auto-direct: services come up by themselves while a direct route is reachable
- Auto-reconnect with back-off; *Disconnect SSH* in one click
- Shared `services.yaml` plus per-machine `local.yaml`
- `tunhub init` and `tunhub service install|uninstall|status`
  (systemd, launchd, Windows Task Scheduler)
