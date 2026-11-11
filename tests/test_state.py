import json

from tunhub.state import StateStore


def test_values_are_kept_per_config_dir(tmp_path):
    path = tmp_path / "state.json"
    one, two = StateStore(tmp_path / "one", path), StateStore(tmp_path / "two", path)
    one.set("order", ["a", "b"])
    two.set("order", ["x"])
    assert StateStore(tmp_path / "one", path).get("order") == ["a", "b"]
    assert two.get("order") == ["x"]
    assert one.get("missing", 5) == 5


def test_none_removes_a_value(tmp_path):
    path = tmp_path / "state.json"
    store = StateStore(tmp_path / "cfg", path)
    store.set("order", ["a"])
    store.set("order", None)
    assert store.get("order") is None
    assert json.loads(path.read_text())["configs"] == {}


def test_a_broken_file_is_treated_as_empty(tmp_path):
    path = tmp_path / "state.json"
    store = StateStore(tmp_path / "cfg", path)
    for content in ("not json", "[1, 2]", '{"configs": 3}'):
        path.write_text(content)
        assert store.get("order") is None
        store.set("order", ["a"])  # and is replaced by a good one
        assert store.get("order") == ["a"]


def test_an_unwritable_location_does_not_raise(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("")
    store = StateStore(tmp_path / "cfg", blocker / "state.json")  # parent is a file
    store.set("order", ["a"])
    assert store.get("order") is None


def test_no_temporary_files_are_left(tmp_path):
    store = StateStore(tmp_path / "cfg", tmp_path / "state" / "state.json")
    store.set("order", ["a"])
    assert [p.name for p in (tmp_path / "state").iterdir()] == ["state.json"]
