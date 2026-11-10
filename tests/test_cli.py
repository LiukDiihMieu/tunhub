from pathlib import Path

import pytest
import yaml

from tunhub import __version__
from tunhub.cli import main
from tunhub.config import load_config


def test_version(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_init_creates_valid_example_and_refuses_overwrite(tmp_path, capsys):
    config_dir = tmp_path / "cfg"
    assert main(["init", "--config-dir", str(config_dir)]) == 0
    assert (config_dir / "local.example.yaml").exists()
    config = load_config(config_dir)  # the shipped example must be valid
    assert "nas" in config.services

    (config_dir / "services.yaml").write_text("services: {}")
    assert main(["init", "--config-dir", str(config_dir)]) == 1
    assert "already exists" in capsys.readouterr().err
    assert main(["init", "--config-dir", str(config_dir), "--force"]) == 0
    assert "nas" in yaml.safe_load((config_dir / "services.yaml").read_text())["services"]


def test_local_example_merges_cleanly(tmp_path):
    main(["init", "--config-dir", str(tmp_path)])
    (tmp_path / "local.testbox.yaml").write_text((tmp_path / "local.example.yaml").read_text())
    config = load_config(tmp_path)
    assert "desktop" not in config.services
    assert config.services["jupyter"].routes[0].kind == "direct"


def test_example_services_cannot_reach_real_machines(tmp_path):
    # Documentation addresses (RFC 5737) and ssh aliases that do not exist, so
    # the examples never connect to a device that happens to be on the LAN.
    import ipaddress

    main(["init", "--config-dir", str(tmp_path)])
    config = load_config(tmp_path)
    assert set(config.services) == {"nas", "jupyter", "desktop"}
    documentation = [ipaddress.ip_network(n) for n in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")]
    for service in config.services.values():
        assert service.name.startswith("Example ")
        for route in service.routes:
            if route.kind == "direct":
                assert any(ipaddress.ip_address(route.target_host) in n for n in documentation)
            else:
                assert route.ssh_host.startswith("example-")


# ------------------------------------------------------------------ CLI contract


@pytest.mark.parametrize(
    "argv",
    [
        ["--config-dir", "X"],            # run is the default command
        ["--config-dir", "X", "run"],
        ["run", "--config-dir", "X"],
    ],
)
def test_config_dir_before_or_after_the_command(argv):
    from tunhub.cli import build_parser, cmd_run

    args = build_parser().parse_args(argv)
    assert args.handler is cmd_run and str(args.config_dir) == "X"


def test_config_dir_given_before_init_reaches_init(tmp_path):
    assert main(["--config-dir", str(tmp_path / "cfg"), "init"]) == 0
    assert (tmp_path / "cfg" / "services.yaml").exists()


def test_init_that_cannot_write_exits_1(tmp_path, capsys):
    (tmp_path / "a-file").write_text("")
    assert main(["init", "--config-dir", str(tmp_path / "a-file" / "cfg")]) == 1
    assert "cannot write" in capsys.readouterr().err


def test_forget_rejects_a_non_ip_and_changes_nothing(tmp_path, capsys):
    text = "# comment\n@revoked 10.0.0.9 aa:bb:cc:dd:ee:09\nnot-an-ip junk\n10.0.0.1 aa:bb:cc:dd:ee:01\n"
    (tmp_path / "known_macs").write_text(text)
    assert main(["forget", "nas", "--config-dir", str(tmp_path)]) == 2
    assert "not an IP address" in capsys.readouterr().err
    assert (tmp_path / "known_macs").read_text() == text  # used to delete the non-IP lines


def test_forget_removes_only_lines_for_that_ip(tmp_path):
    (tmp_path / "known_macs").write_text(
        "# comment\n@revoked 10.0.0.1 aa:bb:cc:dd:ee:09\nnot-an-ip junk\n10.0.0.1 aa:bb:cc:dd:ee:01\n10.0.0.2 aa:bb:cc:dd:ee:02\n"
    )
    assert main(["forget", "10.0.0.1", "--config-dir", str(tmp_path)]) == 0
    assert (tmp_path / "known_macs").read_text() == (
        "# comment\n@revoked 10.0.0.1 aa:bb:cc:dd:ee:09\nnot-an-ip junk\n10.0.0.2 aa:bb:cc:dd:ee:02\n"
    )


@pytest.mark.parametrize("is_installed, code", [(True, 0), (False, 1)])
def test_service_status_exit_code(monkeypatch, capsys, is_installed, code):
    from tunhub import autostart

    monkeypatch.setattr(autostart, "status", lambda: "status text")
    monkeypatch.setattr(autostart, "installed", lambda: is_installed)
    assert main(["service", "status"]) == code
    assert "status text" in capsys.readouterr().out


def test_service_install_always_records_the_config_dir(monkeypatch, tmp_path):
    # The service starts without this shell's environment, so the directory
    # in use here must be written into it even when it is the default one.
    from tunhub import autostart

    seen = []
    monkeypatch.setattr(autostart, "install", lambda config_dir, machine=None: seen.append(config_dir) or "installed")
    # The default directory, e.g. from XDG_CONFIG_HOME set in this shell only;
    # it used to be left out, and the service then read ~/.config/tunhub.
    monkeypatch.delenv("TUNHUB_CONFIG_DIR", raising=False)
    monkeypatch.setattr("tunhub.cli.default_config_dir", lambda: tmp_path)
    assert main(["service", "install"]) == 0
    assert seen == [tmp_path]


def test_service_install_keeps_symlinks_in_the_config_dir(monkeypatch, tmp_path):
    # A dotfiles symlink may be re-pointed later; the service must follow it.
    from tunhub import autostart

    (tmp_path / "real").mkdir()
    try:
        (tmp_path / "link").symlink_to(tmp_path / "real")
    except OSError:  # Windows without the symlink privilege
        pytest.skip("cannot create symlinks here")
    seen = []
    monkeypatch.setattr(autostart, "install", lambda config_dir, machine=None: seen.append(config_dir) or "installed")
    monkeypatch.chdir(tmp_path)
    assert main(["service", "install", "--config-dir", "link"]) == 0
    assert seen == [tmp_path / "link"]
    assert autostart.run_command(Path("link"))[-1] == str(tmp_path / "link")


# ---------------------------------------------------------------------------
# The machine name survives `service install`


def _install_and_start(monkeypatch, tmp_path, argv):
    """Run `service install`, then start the recorded command the way login does:
    without this shell's TUNHUB_MACHINE. Returns (recorded argv, local file read)."""
    from tunhub import autostart, paths
    from tunhub.cli import build_parser
    from tunhub.hub import Hub

    recorded = []
    monkeypatch.setattr(
        autostart, "install", lambda config_dir, machine=None: recorded.append(autostart.run_command(config_dir, machine)) or ""
    )
    monkeypatch.setattr(paths, "_os_machine_name", lambda: "Desk.lan")
    assert main(argv + ["--config-dir", str(tmp_path)]) == 0

    monkeypatch.delenv("TUNHUB_MACHINE", raising=False)  # the login session
    args = build_parser().parse_args(recorded[0][3:])
    return recorded[0], Hub(args.config_dir, tmp_path / "cache", machine=args.machine).config.local_path.name


def test_service_install_records_the_machine_from_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("TUNHUB_MACHINE", "Travel-Laptop")
    argv, local_file = _install_and_start(monkeypatch, tmp_path, ["service", "install"])
    assert argv[-2:] == ["--machine", "travel-laptop"]
    assert local_file == "local.travel-laptop.yaml"


def test_service_install_records_the_machine_option(monkeypatch, tmp_path):
    monkeypatch.setenv("TUNHUB_MACHINE", "ignored")
    argv, local_file = _install_and_start(monkeypatch, tmp_path, ["service", "install", "--machine", "laptop"])
    assert local_file == "local.laptop.yaml"


def test_service_install_follows_the_host_name_by_default(monkeypatch, tmp_path):
    monkeypatch.delenv("TUNHUB_MACHINE", raising=False)
    argv, local_file = _install_and_start(monkeypatch, tmp_path, ["service", "install"])
    assert "--machine" not in argv
    assert local_file == "local.desk.yaml"


def test_machine_option_comes_before_the_environment(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("TUNHUB_MACHINE", "from-env")
    assert main(["--machine", "From-Option", "init", "--config-dir", str(tmp_path)]) == 0
    assert "local.from-option.yaml" in capsys.readouterr().out
