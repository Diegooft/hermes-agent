"""Real deleted descriptors stay visible while diagnostic IO blocks or fails."""
import os
import queue
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.e2e.core.sqlite import _helpers

pytestmark = pytest.mark.platforms("linux")


class OneScan:
    done = False

    def is_set(self):
        return self.done

    def wait(self, _seconds):
        self.done = True


def monitor(db):
    chamber = _helpers.Chamber.__new__(_helpers.Chamber)
    chamber.db, chamber.mode = db, "wal"
    chamber.deleted_hits, chamber.deleted_fd_details = [], []
    chamber._lock, chamber._monitor_stop = threading.Lock(), OneScan()
    chamber._detail_queue = queue.Queue(maxsize=32)
    chamber._detail_keys, chamber._detail_errors = set(), []
    chamber._detail_stop = threading.Event()
    child = SimpleNamespace(pid=os.getpid(), poll=lambda: None)
    chamber.procs = {"owned-control": child}
    chamber.live = lambda: [("owned-control", child)]
    return chamber


def held(tmp_path, count=1):
    path = tmp_path / "state.db-shm"
    path.write_bytes(b"private control")
    descriptors = [os.open(path, os.O_RDONLY) for _ in range(count)]
    path.unlink()
    return descriptors


def test_bounded_async_details_retain_all_real_hits(tmp_path):
    descriptors = held(tmp_path, 40)
    try:
        chamber = monitor(tmp_path / "state.db")
        chamber._scan_loop()
        assert len(chamber.deleted_hits) == 40
        assert chamber._detail_queue.qsize() == 32
        chamber._detail_stop.set()
        chamber._detail_loop()
        details = chamber.deleted_fd_details_snapshot()
        assert len(details) == 32
        for event in details:
            assert event["observed"]["fd_stat_before"]["nlink"] == 0
            assert event["observed"]["fd_identity_stable"] is True
            assert event["same_fd_as_observation"] is True
        retained = chamber.deleted_hits_snapshot()
    finally:
        for fd in descriptors:
            os.close(fd)
    assert chamber.deleted_hits_snapshot() == retained
    assert chamber.deleted_fd_details_snapshot()[0]["same_fd_as_observation"] is False


@pytest.mark.parametrize("error", [PermissionError(13, "denied"), UnicodeError("invalid metadata")])
def test_metadata_exceptions_cannot_hide_real_holder(tmp_path, monkeypatch, error):
    descriptors = held(tmp_path)
    original = Path.read_text

    def unreadable(path, *args, **kwargs):
        if path.parent.name == "fdinfo":
            raise error
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", unreadable)
    try:
        chamber = monitor(tmp_path / "state.db")
        chamber._scan_loop()
        chamber._detail_stop.set()
        chamber._detail_loop()
        assert chamber.deleted_hits_snapshot()
        details = chamber.deleted_fd_details_snapshot()
        assert details[0]["observed"]["fdinfo"]["error"] == type(error).__name__
        assert not chamber._detail_errors
    finally:
        os.close(descriptors[0])


def test_blocked_collector_does_not_block_invariant_scan(tmp_path, monkeypatch):
    descriptors = held(tmp_path, 2)
    entered, release = threading.Event(), threading.Event()
    original = _helpers._deleted_fd_observation

    def blocked(*args):
        entered.set()
        assert release.wait(5), "test failed to release collector"
        return original(*args)

    monkeypatch.setattr(_helpers, "_deleted_fd_observation", blocked)
    chamber = monitor(tmp_path / "state.db")
    worker = threading.Thread(target=chamber._detail_loop)
    worker.start()
    try:
        chamber._scan_loop()
        chamber._detail_stop.set()
        assert entered.wait(2)
        # While diagnostic IO is blocked, both real FDs have already been recorded.
        assert len(chamber.deleted_hits) == 2
        for fd in descriptors:
            os.close(fd)
        descriptors.clear()
        assert chamber.deleted_hits_snapshot()
    finally:
        release.set()
        worker.join(5)
        for fd in descriptors:
            os.close(fd)
    assert not worker.is_alive()


def test_unexpected_collector_failure_is_retained_and_queue_drains(tmp_path, monkeypatch):
    descriptors = held(tmp_path, 2)

    def broken(*_args):
        raise RuntimeError("diagnostic failure control")

    monkeypatch.setattr(_helpers, "_deleted_fd_observation", broken)
    try:
        chamber = monitor(tmp_path / "state.db")
        chamber._scan_loop()
        chamber._detail_stop.set()
        chamber._detail_loop()
        assert len(chamber.deleted_hits) == 2
        assert chamber._detail_errors == ["RuntimeError", "RuntimeError"]
        assert chamber._detail_queue.unfinished_tasks == 0
    finally:
        for fd in descriptors:
            os.close(fd)


def test_shutdown_keeps_consumer_alive_until_late_producer_finishes(tmp_path):
    chamber = monitor(tmp_path / "state.db")
    chamber._monitor_stop = threading.Event()
    # No real children to stop; the controlled producer owns the queued diagnostic event.
    chamber.procs = {}
    chamber.live = lambda: []
    producer_ready, release_producer = threading.Event(), threading.Event()
    errors = []

    def late_producer():
        try:
            assert chamber._monitor_stop.wait(2)
            producer_ready.set()
            assert release_producer.wait(3)
            chamber._detail_queue.put_nowait(("already-exited", SimpleNamespace(pid=0, poll=lambda: 0),
                                             "0", "private (deleted)", 0))
        except Exception as error:
            errors.append(error)

    def stop():
        try:
            chamber.shutdown()
        except Exception as error:
            errors.append(error)

    chamber._monitor = threading.Thread(target=late_producer)
    chamber._detail_worker = threading.Thread(target=chamber._detail_loop)
    chamber._monitor.start()
    chamber._detail_worker.start()
    shutdown = threading.Thread(target=stop)
    shutdown.start()
    try:
        assert producer_ready.wait(2)
        # Producer is still running, so the consumer must remain unsignalled.
        assert not chamber._detail_stop.is_set()
    finally:
        release_producer.set()
        shutdown.join(6)
        chamber._monitor.join(2)
        chamber._detail_worker.join(2)
    assert not errors
    assert not shutdown.is_alive()
    assert chamber._detail_stop.is_set()
    assert not chamber._detail_worker.is_alive()
    assert chamber._detail_queue.unfinished_tasks == 0
    assert len(chamber.deleted_fd_details) == 1
