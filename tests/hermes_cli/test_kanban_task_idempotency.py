"""Behavior contracts for task idempotency under concurrent creators."""

from __future__ import annotations

import contextlib
import threading
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def test_create_task_idempotency_race_serializes_after_two_readonly_misses(kanban_home, monkeypatch):
    real_write_txn = kb.write_txn
    barrier = threading.Barrier(2)
    local = threading.local()

    @contextlib.contextmanager
    def synchronized_write_txn(conn, *, allow_nested=False):
        if not getattr(local, "entered", False):
            local.entered = True
            barrier.wait(timeout=5)
        with real_write_txn(conn, allow_nested=allow_nested):
            yield

    monkeypatch.setattr(kb, "write_txn", synchronized_write_txn)
    results = [None, None]
    errors = []

    def create(slot):
        try:
            with kbc.connect_closing() as conn:
                results[slot] = kb.create_task(
                    conn, title=f"race {slot}", idempotency_key="race-key",
                )
        except BaseException as exc:
            errors.append(exc)

    workers = [threading.Thread(target=create, args=(slot,)) for slot in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)
        assert not worker.is_alive()

    assert not errors
    assert results[0] == results[1]
    with kbc.connect_closing() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE idempotency_key = ? AND status != 'archived'",
            ("race-key",),
        ).fetchone()[0] == 1


def test_create_task_idempotency_compatibility_contract(kanban_home, monkeypatch):
    with kbc.connect_closing() as conn:
        first = kb.create_task(
            conn, title="first", idempotency_key="shared-key", tenant="tenant-a",
        )
        board_reads = []
        original_board_meta = kb._board_meta_for

        def observed_board_meta(board):
            board_reads.append(board)
            return original_board_meta(board)

        def no_write_transaction(*_args, **_kwargs):
            raise AssertionError("existing idempotency key must use the read-only fast path")

        with monkeypatch.context() as patch:
            patch.setattr(kb, "_board_meta_for", observed_board_meta)
            patch.setattr(kb, "write_txn", no_write_transaction)
            assert kb.create_task(
                conn,
                title="readonly retry",
                idempotency_key="shared-key",
                workspace_kind="dir",
                board="fastpath-board",
            ) == first
        assert board_reads == ["fastpath-board"]
        assert kb.create_task(
            conn, title="tenant retry", idempotency_key="shared-key", tenant="tenant-b",
        ) == first
        assert kb.create_task(conn, title="distinct", idempotency_key="other-key") != first
        assert kb.archive_task(conn, first)
        replacement = kb.create_task(conn, title="replacement", idempotency_key="shared-key")
        assert replacement != first
        with kb.write_txn(conn):
            nested = kb.create_task(conn, title="nested", idempotency_key="nested-key")
            assert kb.create_task(conn, title="nested retry", idempotency_key="nested-key") == nested

    with kbc.connect_closing(board="idempotency-board-a") as board_a:
        task_a = kb.create_task(
            board_a, title="board a", idempotency_key="board-key", workspace_kind="scratch",
        )
    with kbc.connect_closing(board="idempotency-board-b") as board_b:
        task_b = kb.create_task(
            board_b, title="board b", idempotency_key="board-key", workspace_kind="scratch",
        )
    assert task_a != task_b