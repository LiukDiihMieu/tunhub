"""Command line: ``tunhub [run|init|service] ...``."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from importlib import resources
from pathlib import Path

from . import __version__
from .config import LOCAL_FILE, SHARED_FILE, ConfigError
from .known_macs import FILE_NAME as KNOWN_MACS_FILE
from .known_macs import KnownMacs
from .winjob import kill_children_on_exit
from .paths import CONFIG_DIR_ENV, default_config_dir, default_log_file

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
    parser.set_defaults(handler=cmd_run, config_dir=None, log_file=None)

    config_option = argparse.ArgumentParser(add_help=False)
    config_option.add_argument(
        "--config-dir",
        type=Path,
        help=f"directory with {SHARED_FILE} and optional {LOCAL_FILE} "
        f"(default: ${CONFIG_DIR_ENV} or {default_config_dir()})",
    )

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
        print(
            f"tunhub: no {SHARED_FILE} in {config_dir}\n"
            f"        create an example with: tunhub init"
            + (f" --config-dir {config_dir}" if args.config_dir else ""),
            file=sys.stderr,
        )
        return 2

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

    config_dir.mkdir(parents=True, exist_ok=True)
    templates = resources.files("tunhub") / "templates"
    for name in TEMPLATES:
        (config_dir / name).write_text((templates / name).read_text(encoding="utf-8"), encoding="utf-8")

    print(f"created {target}")
    print(f"created {config_dir / 'local.example.yaml'}")
    print("next: edit the example services, then run `tunhub` and open http://hub.localhost:8765")
    return 0


def cmd_service(args: argparse.Namespace) -> int:
    from .autostart import AutostartError, install, status, uninstall

    # Bake a non-default config dir into the service; the service won't see this shell's env.
    explicit = args.config_dir or (Path(os.environ[CONFIG_DIR_ENV]) if os.environ.get(CONFIG_DIR_ENV) else None)
    try:
        if args.action == "install":
            config_dir = explicit or default_config_dir()
            if not (config_dir / SHARED_FILE).exists():
                print(f"tunhub: warning: no {SHARED_FILE} in {config_dir} yet (run `tunhub init`)", file=sys.stderr)
            print(install(explicit))
        elif args.action == "uninstall":
            print(uninstall())
        else:
            print(status())
    except AutostartError as exc:
        print(f"tunhub: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_forget(args: argparse.Namespace) -> int:
    path = resolve_config_dir(args) / KNOWN_MACS_FILE
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
