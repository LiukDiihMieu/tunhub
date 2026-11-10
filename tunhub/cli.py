"""Command line: ``tunhub [run|init|service|forget] ...``.

Exit codes (part of the compatibility policy): 0 success; 1 the operation
could not be done (e.g. a file could not be written, autostart failed, the
autostart entry is not installed); 2 invalid arguments or configuration.
"""

from __future__ import annotations

import argparse
import ipaddress
import logging
import os
import sys
from importlib import resources
from pathlib import Path

from . import __version__
from .config import DEFAULT_HUB_PORT, LOCAL_FILE, SHARED_FILE, ConfigError, local_file_name
from .known_macs import FILE_NAME as KNOWN_MACS_FILE
from .known_macs import KnownMacs
from .winjob import kill_children_on_exit
from .paths import CONFIG_DIR_ENV, default_config_dir, default_log_file, machine_name

TEMPLATES = ("services.yaml", "local.example.yaml")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "handler", cmd_run)
    return handler(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tunhub",
        description="One local dashboard for your SSH-tunnelled and LAN services.",
    )
    parser.add_argument("--version", action="version", version=f"tunhub {__version__}")
    config_help = (
        f"directory with {SHARED_FILE} and optional {LOCAL_FILE} "
        f"(default: ${CONFIG_DIR_ENV} or {default_config_dir()})"
    )
    # Also accepted before the command: `tunhub --config-dir X` (run is the default).
    parser.add_argument("--config-dir", type=Path, help=config_help)
    parser.set_defaults(handler=cmd_run, config_dir=None, log_file=None)

    config_option = argparse.ArgumentParser(add_help=False)
    # SUPPRESS: without the option, a subcommand must not overwrite a value
    # given before the command (`tunhub --config-dir X run`).
    config_option.add_argument("--config-dir", type=Path, default=argparse.SUPPRESS, help=config_help)

    commands = parser.add_subparsers(title="commands", metavar="COMMAND")

    run = commands.add_parser("run", parents=[config_option], help="start the dashboard (default)")
    run.add_argument("--log-file", type=Path, help="append output to this file")
    run.set_defaults(handler=cmd_run)

    init = commands.add_parser("init", parents=[config_option], help="create an example configuration")
    init.add_argument("--force", action="store_true", help=f"overwrite an existing {SHARED_FILE}")
    init.set_defaults(handler=cmd_init)

    service = commands.add_parser("service", parents=[config_option], help="start tunhub at login")
    service.add_argument("action", choices=("install", "uninstall", "status"))
    service.set_defaults(handler=cmd_service)

    forget = commands.add_parser(
        "forget",
        parents=[config_option],
        help="forget the known MAC of a direct target (like ssh-keygen -R)",
    )
    forget.add_argument("ip", help="the direct target's IP address, as in known_macs")
    forget.set_defaults(handler=cmd_forget)
    return parser


def resolve_config_dir(args: argparse.Namespace) -> Path:
    return args.config_dir or default_config_dir()


# ---------------------------------------------------------------------------
# Commands


def cmd_run(args: argparse.Namespace) -> int:
    # pythonw (Windows) and launchd have no usable console; never crash on print.
    log_file = args.log_file
    if log_file is None and sys.stderr is None:
        log_file = default_log_file()
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        stream = open(log_file, "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = stream

    config_dir = resolve_config_dir(args)
    if not (config_dir / SHARED_FILE).exists():
        # Start anyway: the dashboard explains how to create the configuration.
        print(
            f"tunhub: no {SHARED_FILE} in {config_dir} yet; the dashboard shows how to start\n"
            f"        (or create an example with: tunhub init"
            + (f" --config-dir {config_dir}" if args.config_dir else "")
            + ")",
            file=sys.stderr,
        )

    # Imported lazily so that `tunhub init/service` stay fast.
    import uvicorn

    from .app import create_app
    from .hub import Hub

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        hub = Hub(config_dir)
    except ConfigError as exc:
        print(f"tunhub: config error: {exc}", file=sys.stderr)
        return 2

    # Windows: ssh processes end with tunhub, even if tunhub is killed.
    kill_children_on_exit()

    settings = hub.hub_settings
    print(f"tunhub {__version__}: config {config_dir}", file=sys.stderr)
    print(f"tunhub: dashboard at http://{settings.hostname}:{settings.port}", file=sys.stderr)
    uvicorn.run(create_app(hub), host=settings.listen_host, port=settings.port, log_level="warning")
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    config_dir = resolve_config_dir(args)
    target = config_dir / SHARED_FILE
    if target.exists() and not args.force:
        print(f"tunhub: {target} already exists (use --force to overwrite)", file=sys.stderr)
        return 1

    templates = resources.files("tunhub") / "templates"
    try:
        config_dir.mkdir(parents=True, exist_ok=True)
        for name in TEMPLATES:
            (config_dir / name).write_text((templates / name).read_text(encoding="utf-8"), encoding="utf-8")
    except OSError as exc:
        print(f"tunhub: cannot write to {config_dir}: {exc}", file=sys.stderr)
        return 1

    print(f"created {target}")
    print(f"created {config_dir / 'local.example.yaml'}")
    print(f"this machine's overrides go in {local_file_name(machine_name())} (see local.example.yaml)")
    print(f"next: edit the example services, then run `tunhub` and open http://hub.localhost:{DEFAULT_HUB_PORT}")
    return 0


def cmd_service(args: argparse.Namespace) -> int:
    from . import autostart

    try:
        if args.action == "install":
            # Always record the config dir this shell uses: the service starts
            # without this shell's environment (TUNHUB_CONFIG_DIR, XDG_CONFIG_HOME).
            config_dir = Path(os.path.abspath(resolve_config_dir(args)))
            if not (config_dir / SHARED_FILE).exists():
                print(f"tunhub: warning: no {SHARED_FILE} in {config_dir} yet (run `tunhub init`)", file=sys.stderr)
            print(autostart.install(config_dir))
        elif args.action == "uninstall":
            print(autostart.uninstall())
        else:
            print(autostart.status())
            return 0 if autostart.installed() else 1
    except autostart.AutostartError as exc:
        print(f"tunhub: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_forget(args: argparse.Namespace) -> int:
    path = resolve_config_dir(args) / KNOWN_MACS_FILE
    try:
        ipaddress.ip_address(args.ip)
    except ValueError:
        print(f"tunhub: {args.ip!r} is not an IP address (known_macs lists IP addresses)", file=sys.stderr)
        return 2
    try:
        removed = KnownMacs(path).forget(args.ip)
    except (OSError, ValueError) as exc:
        print(f"tunhub: cannot update {path}: {exc}", file=sys.stderr)
        return 1
    if removed:
        print(f"removed {removed} line(s) for {args.ip} from {path}")
        print("its MAC is learned again on the next direct connection")
    else:
        print(f"no entry for {args.ip} in {path}")
    return 0
