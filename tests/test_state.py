import pytest
from agybot.state import Store, ThreadRow


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "state.db"))
    yield s
    s.close()


def test_unknown_thread_returns_none(store):
    assert store.get_thread("nope") is None


def test_created_thread_round_trips(store):
    store.create_thread("t1", "/srv/agy/app", "u1")
    row = store.get_thread("t1")
    assert isinstance(row, ThreadRow)
    assert row.thread_id == "t1"
    assert row.workspace == "/srv/agy/app"
    assert row.created_by == "u1"


def test_conversation_id_starts_null(store):
    store.create_thread("t1", "/srv/agy/app", "u1")
    assert store.get_thread("t1").conversation_id is None


def test_set_conversation_persists(store):
    store.create_thread("t1", "/srv/agy/app", "u1")
    store.set_conversation("t1", "c-abc")
    assert store.get_thread("t1").conversation_id == "c-abc"


def test_touch_advances_last_used(store):
    store.create_thread("t1", "/srv/agy/app", "u1")
    before = store.get_thread("t1").last_used_at
    store.touch("t1")
    assert store.get_thread("t1").last_used_at >= before


def test_state_survives_reopen(tmp_path):
    path = str(tmp_path / "state.db")
    s1 = Store(path)
    s1.create_thread("t1", "/srv/agy/app", "u1")
    s1.set_conversation("t1", "c-abc")
    s1.close()

    s2 = Store(path)
    assert s2.get_thread("t1").conversation_id == "c-abc"
    s2.close()


def test_wal_mode_is_enabled(store):
    mode = store._db.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_creating_same_thread_twice_is_idempotent(store):
    a = store.create_thread("t1", "/srv/agy/app", "u1")
    b = store.create_thread("t1", "/srv/agy/other", "u2")
    assert b.workspace == a.workspace
    assert b.created_by == a.created_by
