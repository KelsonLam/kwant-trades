import pickle

from kwanttrades.data.cache import DiskCache


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_roundtrip_and_ttl(tmp_path):
    clk = Clock()
    c = DiskCache(tmp_path, clock=clk)
    c.set("chain:SPY:0:45", {"a": 1})
    assert c.get("chain:SPY:0:45", 60) == {"a": 1}
    clk.t += 61
    assert c.get("chain:SPY:0:45", 60) is None
    assert c.get("chain:SPY:0:45", None) == {"a": 1}  # no TTL = never expires


def test_windows_unsafe_keys(tmp_path):
    c = DiskCache(tmp_path)
    key = 'bars:^IRX:1m:<>:"|?*'
    c.set(key, 5)
    assert c.get(key, 10) == 5


def test_corrupt_entry_is_ignored_and_removed(tmp_path):
    c = DiskCache(tmp_path)
    c.set("k", 1)
    path = c._path("k")
    path.write_bytes(b"\x80not a pickle")
    assert c.get("k", 10, default="miss") == "miss"
    assert not path.exists()
    path.write_bytes(b"")
    assert c.get("k", 10) is None


def test_wrong_shape_entry_ignored(tmp_path):
    c = DiskCache(tmp_path)
    c._path("k").write_bytes(pickle.dumps("just a string"))
    assert c.get("k", 10) is None


def test_get_or_set_calls_factory_once(tmp_path):
    c = DiskCache(tmp_path)
    calls = []

    def f():
        calls.append(1)
        return None

    c.get_or_set("k", 10, f)
    c.get_or_set("k", 10, f)
    assert len(calls) == 1  # a cached None still counts as a hit


def test_unwritable_dir_degrades(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    c = DiskCache(blocker / "sub")  # cannot be created
    c.set("k", 1)
    assert c.get("k", 10) is None
