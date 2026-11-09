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


def test_run_without_config_points_to_init(tmp_path, capsys):
    assert main(["run", "--config-dir", str(tmp_path / "missing")]) == 2
    assert "tunhub init" in capsys.readouterr().err
