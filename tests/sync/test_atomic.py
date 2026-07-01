import os

from observational_memory.sync.atomic import DirectoryLock


def test_directory_lock_reclaims_dead_owner_pid(monkeypatch, tmp_path):
    lock_path = tmp_path / "materialize.lock"
    lock_path.mkdir()
    (lock_path / "owner").write_text("pid=999999\ncreated=123\n")

    def fake_kill(pid, sig):
        assert pid == 999999
        assert sig == 0
        raise ProcessLookupError

    monkeypatch.setattr(os, "kill", fake_kill)

    lock = DirectoryLock(lock_path, timeout_seconds=0, stale_seconds=3600)
    lock.acquire()

    try:
        assert (lock_path / "owner").read_text().startswith(f"pid={os.getpid()}\n")
    finally:
        lock.release()

    assert not lock_path.exists()
