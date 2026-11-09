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


@pytest.mark.parametrize(
    "raw, problem",
    [
        ({"services": ["oops"]}, "services: expected a mapping"),
        ({"hub": 5}, "hub: expected a mapping"),
        ({"tunnel": ["x"]}, "tunnel: expected a mapping"),
        ({"tunnel": {"ssh_options": ["x"]}}, "tunnel.ssh_options: expected a mapping"),
        ({"hub": {"open_timeout": "oops"}}, "hub.open_timeout: expected a number"),
        ({"tunnel": {"auto_direct_interval": -1}}, "tunnel.auto_direct_interval: must be a positive"),
        ({"tunnel": {"probe_timeout": 0}}, "tunnel.probe_timeout: must be a positive"),
        ({"tunnel": {"ready_timeout": float("inf")}}, "tunnel.ready_timeout: must be a positive"),
        ({"tunnel": {"auto_direct": "false"}}, "tunnel.auto_direct: expected true or false"),
        ({"services": {"dsm": {**BASE_SERVICE, "autostart": "false"}}}, "autostart: expected true or false"),
        ({"services": {"dsm": {**BASE_SERVICE, "enabled": "false"}}}, "enabled: expected true or false"),
        ({"services": {"dsm": {**BASE_SERVICE, "local_port": True}}}, "invalid port True"),
        ({"tunnel": {"ssh_command": 42}}, "tunnel.ssh_command: expected a string"),
        ({"hub": {"port": float("inf")}}, "hub.port: invalid port inf"),
        ({"hub": {"port": 8765.7}}, "hub.port: invalid port 8765.7"),  # used to become 8765
    ],
)
def test_malformed_config_raises_config_error(raw, problem):
    # Anything but ConfigError would crash `tunhub run` or make reload return 500.
    with pytest.raises(ConfigError, match=problem):
        parse_config(raw)


BAD_VALUES = [42, -1, 1.5, float("inf"), float("nan"), "oops", "", "²", "５０００", [], ["x"], {}, {"a": 1}, True, False, None]
VALID = {
    "hub": {"listen": "127.0.0.1", "port": 8765, "hostname": "hub.localhost", "open_timeout": 40},
    "tunnel": {
        "ssh_command": "ssh",
        "ssh_options": {"ServerAliveInterval": 15},
        "probe_timeout": 0.8,
        "ready_timeout": 15,
        "health_timeout": 8,
        "auto_direct": True,
        "auto_direct_interval": 15,
    },
    "services": {
        "nas": {
            "name": "NAS",
            "type": "http",
            "local_port": 15000,
            "hostname": "nas.localhost",
            "path": "/",
            "autostart": False,
            "health_check": True,
            "description": "",
            "auto_direct": True,
            "icon": "nas.png",
            "enabled": True,
            "routes": [
                {"direct": "192.168.1.10:5000", "label": "home"},
                {"ssh": "nas", "remote": "127.0.0.1:5000", "label": "ssh"},
            ],
        }
    },
}


def _key_paths(node, prefix=()):
    """Every path in VALID, e.g. ('services', 'nas', 'routes', 1, 'remote')."""
    items = node.items() if isinstance(node, dict) else enumerate(node)
    for key, value in items:
        yield prefix + (key,)
        if isinstance(value, (dict, list)):
            yield from _key_paths(value, prefix + (key,))


def _replaced(path, value):
    import copy

    raw = copy.deepcopy(VALID)
    node = raw
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    return raw


@pytest.mark.parametrize("path", list(_key_paths(VALID)), ids=lambda p: ".".join(map(str, p)))
def test_any_bad_value_is_accepted_or_raises_config_error(path):
    # Whatever the user writes, tunhub must either accept it or report a
    # ConfigError; any other exception crashes startup or fails a reload with 500.
    for value in BAD_VALUES:
        try:
            parse_config(_replaced(path, value))
        except ConfigError:
            pass
        except Exception as exc:  # noqa: BLE001
            pytest.fail(f"{'.'.join(map(str, path))} = {value!r} raised {type(exc).__name__}: {exc}")


# Every place a port is written, through one rule (_check_port).
PORT_PATHS = {
    "hub.port": lambda port: parse_config({"hub": {"port": port}}).hub.port,
    "local_port": lambda port: parse_config({"services": {"s": {**BASE_SERVICE, "local_port": port}}})
    .services["s"].local_port,
    "direct host:port": lambda port: parse_endpoint(f"192.168.1.10:{port}", "r")[1],
    "remote bare port": lambda port: parse_endpoint(port, "r", default_host="127.0.0.1")[1],
    "remote [v6]:port": lambda port: parse_endpoint(f"[::1]:{port}", "r")[1],
}


@pytest.mark.parametrize("port", ["²", "５０００", "٥٠٠٠", "5000.0", "-1", "0", "65536", "50 00"])
@pytest.mark.parametrize("path", PORT_PATHS)
def test_invalid_port_is_rejected_on_every_path(path, port):
    # '²' used to crash (str.isdigit() accepts it, int() does not), and
    # full-width digits were accepted on some paths but not others.
    with pytest.raises(ConfigError):
        PORT_PATHS[path](port)


@pytest.mark.parametrize("path", PORT_PATHS)
def test_valid_port_is_accepted_on_every_path(path):
    assert PORT_PATHS[path]("5000") == 5000


@pytest.mark.parametrize("body", [False, 0, [], ""])
def test_service_body_must_be_a_mapping(body):
    # Used to become {} and then report "local_port is required".
    with pytest.raises(ConfigError, match="service 'nas': expected a mapping"):
        parse_config({"services": {"nas": body}})


def test_null_service_body_still_means_empty_mapping():
    with pytest.raises(ConfigError, match="local_port is required"):
        parse_config({"services": {"nas": None}})


@pytest.mark.parametrize("content", ["false", "0", "[]", '""', "- a"])
@pytest.mark.parametrize("file_name", ["services.yaml", "local.yaml"])
def test_config_file_top_level_must_be_a_mapping(tmp_path, file_name, content):
    # Used to be read as an empty config, so a broken local.yaml was ignored.
    (tmp_path / "services.yaml").write_text("services: {}\n")
    (tmp_path / file_name).write_text(content + "\n")
    with pytest.raises(ConfigError, match="top level: expected a mapping"):
        load_config(tmp_path)


@pytest.mark.parametrize("file_name", ["services.yaml", "local.yaml"])
def test_empty_config_file_is_an_empty_mapping(tmp_path, file_name):
    (tmp_path / "services.yaml").write_text("services: {}\n")
    (tmp_path / file_name).write_text("# only a comment\n")
    assert load_config(tmp_path).services == {}
