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
    (tmp_path / "local.yaml").write_text((tmp_path / "local.example.yaml").read_text())
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
    monkeypatch.setattr(autostart, "install", lambda config_dir: seen.append(config_dir) or "installed")
    # The default directory, e.g. from XDG_CONFIG_HOME set in this shell only;
    # it used to be left out, and the service then read ~/.config/tunhub.
    monkeypatch.delenv("TUNHUB_CONFIG_DIR", raising=False)
    monkeypatch.setattr("tunhub.cli.default_config_dir", lambda: tmp_path)
    assert main(["service", "install"]) == 0
    assert seen == [tmp_path.resolve()]
