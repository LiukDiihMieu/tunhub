import pytest

from tunhub.config import ConfigError, load_config, parse_config, parse_endpoint
from tunhub.ssh import build_ssh_command

BASE_SERVICE = {"local_port": 15000, "routes": [{"direct": "192.168.1.10:5000"}]}


def test_defaults_for_web_service():
    config = parse_config({"services": {"dsm": BASE_SERVICE}})
    dsm = config.services["dsm"]
    assert dsm.hostname == "dsm.localhost"
    assert dsm.url == "http://dsm.localhost:15000/"
    assert dsm.health_check is True
    assert config.hub.allowed_hosts == {"hub.localhost:8765", "localhost:8765", "127.0.0.1:8765"}


def test_tcp_service_uses_loopback_address_and_no_health_check():
    raw = {"local_port": 13389, "type": "tcp", "routes": [{"ssh": "lab", "remote": 3390}]}
    rdp = parse_config({"services": {"rdp": raw}}).services["rdp"]
    assert rdp.url is None
    assert rdp.address == "127.0.0.1:13389"
    assert rdp.health_check is False
    route = rdp.routes[0]
    assert (route.kind, route.ssh_host, route.target_host, route.target_port) == ("ssh", "lab", "127.0.0.1", 3390)


def test_local_yaml_overrides_and_disables(tmp_path):
    (tmp_path / "services.yaml").write_text(
        """
services:
  dsm:
    name: DSM
    local_port: 15000
    routes: [{direct: "192.168.1.10:5000"}, {ssh: nas, remote: "127.0.0.1:5000"}]
  jupyter:
    local_port: 18888
    routes: [{direct: "127.0.0.1:8888"}]
"""
    )
    (tmp_path / "local.yaml").write_text(
        """
services:
  dsm:
    routes: [{ssh: nas, remote: 5000}]
  jupyter:
    enabled: false
"""
    )
    config = load_config(tmp_path)
    assert list(config.services) == ["dsm"]
    dsm = config.services["dsm"]
    assert dsm.name == "DSM"  # untouched keys survive the merge
    assert [r.kind for r in dsm.routes] == ["ssh"]  # lists are replaced


def test_duplicate_local_port_is_rejected():
    services = {"a": BASE_SERVICE, "b": BASE_SERVICE}
    with pytest.raises(ConfigError, match="already used"):
        parse_config({"services": services})


def test_local_port_may_not_clash_with_hub():
    with pytest.raises(ConfigError, match="hub"):
        parse_config({"hub": {"port": 15000}, "services": {"dsm": BASE_SERVICE}})


@pytest.mark.parametrize(
    "raw, problem",
    [
        ({"routes": [{"direct": "x:1"}]}, "local_port"),
        ({"local_port": 1, "routes": []}, "routes"),
        ({"local_port": 1, "routes": [{"ssh": "nas"}]}, "remote"),
        ({"local_port": 1, "type": "ftp", "routes": [{"direct": "x:1"}]}, "type"),
        ({"local_port": 70000, "routes": [{"direct": "x:1"}]}, "range"),
    ],
)
def test_invalid_services(raw, problem):
    with pytest.raises(ConfigError, match=problem):
        parse_config({"services": {"svc": raw}})


def test_parse_endpoint_variants():
    assert parse_endpoint("10.0.0.2:22", "t") == ("10.0.0.2", 22)
    assert parse_endpoint("[::1]:8888", "t") == ("::1", 8888)
    assert parse_endpoint(5000, "t", default_host="127.0.0.1") == ("127.0.0.1", 5000)
    with pytest.raises(ConfigError):
        parse_endpoint(5000, "t")


def test_ssh_command_user_options_come_first():
    config = parse_config(
        {
            "tunnel": {"ssh_options": {"BatchMode": False}},
            "services": {"rdp": {"local_port": 13389, "routes": [{"ssh": "lab", "remote": "[::1]:3390"}]}},
        }
    )
    argv = build_ssh_command(config.tunnel, config.services["rdp"].routes[0], 13389)
    assert argv[:3] == ["ssh", "-N", "-T"]
    assert argv[-3:] == ["-L", "127.0.0.1:13389:[::1]:3390", "lab"]
    # ssh honours the first value of an option, so the user's choice must precede ours.
    assert argv.index("BatchMode=no") < argv.index("BatchMode=yes")
