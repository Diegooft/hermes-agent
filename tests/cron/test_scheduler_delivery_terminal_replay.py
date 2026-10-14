"""Reconnect queueing respects the delivery ledger's terminal decision."""

from types import SimpleNamespace
import threading

import pytest

from cron import scheduler_delivery as delivery
from gateway import delivery_ledger as ledger


@pytest.fixture
def isolated_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "_db_path", lambda: tmp_path / "state.db")


@pytest.mark.parametrize("state", ["delivered", "abandoned"])
def test_reconnect_queue_does_not_reopen_terminal_row(isolated_ledger, state):
    target = delivery._TargetDelivery(
        job={"id": "job"}, platform=SimpleNamespace(value="telegram"), platform_name="telegram", chat_id="chat",
        thread_id=None, transport=SimpleNamespace(adapter=SimpleNamespace(_owner_profile="default")), pconfig=None,
        runtime_adapter=None, target_adapters=None, config=None, loop=None, notify_delivery=False, origin={},
        origin_target=False, origin_user_id=None, is_dm_target=True, mirror_text="", mirror_this_target=False,
        in_channel_surface=False, inchannel_continuable=False, opened_thread_id=None, live_error="send_path_degraded")
    oid = ledger.compute_obligation_id("cron:telegram:chat", "job:job", "answer")
    ledger.record_obligation(obligation_id=oid, session_key="cron:telegram:chat", platform="telegram", chat_id="chat",
                             thread_id=None, content="answer")
    if state == "delivered":
        ledger.mark_delivered(oid)
    else:
        with ledger._connect() as conn:
            conn.execute("UPDATE delivery_obligations SET state='abandoned', attempts=? WHERE obligation_id=?",
                         (ledger.MAX_ATTEMPTS, oid))
    errors = []

    delivery._queue_for_live_reconnect(target, "answer", [], errors)

    assert errors == []
    with ledger._connect() as conn:
        assert conn.execute("SELECT state, attempts FROM delivery_obligations WHERE obligation_id=?", (oid,)).fetchone() == (
            state, 0 if state == "delivered" else ledger.MAX_ATTEMPTS)


@pytest.mark.parametrize("terminal", ["delivered", "abandoned"])
def test_terminalized_after_observation_adds_no_reconnect_note(isolated_ledger, monkeypatch, terminal):
    target = delivery._TargetDelivery(
        job={"id": "job"}, platform=SimpleNamespace(value="telegram"), platform_name="telegram", chat_id="chat",
        thread_id=None, transport=SimpleNamespace(adapter=SimpleNamespace(_owner_profile="default")), pconfig=None,
        runtime_adapter=None, target_adapters=None, config=None, loop=None, notify_delivery=False, origin={},
        origin_target=False, origin_user_id=None, is_dm_target=True, mirror_text="", mirror_this_target=False,
        in_channel_surface=False, inchannel_continuable=False, opened_thread_id=None, live_error="send_path_degraded")
    oid = ledger.compute_obligation_id("cron:telegram:chat", "job:job", "answer")
    ledger.record_obligation(obligation_id=oid, session_key="cron:telegram:chat", platform="telegram", chat_id="chat",
                             thread_id=None, content="answer")
    ledger.mark_failed(oid, "temporary")
    with ledger._connect() as conn:
        conn.execute("UPDATE delivery_obligations SET owner_pid=?, owner_started_at=? WHERE obligation_id=?",
                     (999999999, 1, oid))
    observed, continue_claim, errors = threading.Event(), threading.Event(), []

    def dead_owner_after_barrier(*_):
        observed.set()
        assert continue_claim.wait(2)
        return False

    monkeypatch.setattr(ledger, "_owner_alive", dead_owner_after_barrier)
    worker = threading.Thread(target=delivery._queue_for_live_reconnect, args=(target, "answer", [], errors))
    worker.start()
    assert observed.wait(2)
    with ledger._connect() as conn:
        conn.execute("UPDATE delivery_obligations SET state=? WHERE obligation_id=?", (terminal, oid))
    continue_claim.set()
    worker.join(2)

    assert errors == []
    with ledger._connect() as conn:
        assert conn.execute("SELECT state FROM delivery_obligations WHERE obligation_id=?", (oid,)).fetchone() == (terminal,)


@pytest.mark.parametrize("initial", ["new", "failed_same_owner", "failed_dead_owner"])
def test_reconnect_success_persists_and_reports_queue(isolated_ledger, initial, caplog):
    target = delivery._TargetDelivery(
        job={"id": "job"}, platform=SimpleNamespace(value="telegram"), platform_name="telegram", chat_id="chat",
        thread_id=None, transport=SimpleNamespace(adapter=SimpleNamespace(_owner_profile="default")), pconfig=None,
        runtime_adapter=None, target_adapters=None, config=None, loop=None, notify_delivery=False, origin={},
        origin_target=False, origin_user_id=None, is_dm_target=True, mirror_text="", mirror_this_target=False,
        in_channel_surface=False, inchannel_continuable=False, opened_thread_id=None, live_error="send_path_degraded")
    oid = ledger.compute_obligation_id("cron:telegram:chat", "job:job", "answer")
    if initial != "new":
        ledger.record_obligation(obligation_id=oid, session_key="cron:telegram:chat", platform="telegram", chat_id="chat",
                                 thread_id=None, content="answer")
        ledger.mark_failed(oid, "temporary")
        if initial == "failed_dead_owner":
            with ledger._connect() as conn:
                conn.execute("UPDATE delivery_obligations SET owner_pid=?, owner_started_at=? WHERE obligation_id=?",
                             (999999999, 1, oid))
    errors = []

    delivery._queue_for_live_reconnect(target, "answer", [], errors)

    with ledger._connect() as conn:
        row = conn.execute("SELECT state, attempts, owner_pid, owner_started_at FROM delivery_obligations WHERE obligation_id=?", (oid,)).fetchone()
    assert row[:2] == ("failed", 0)
    assert row[2:] == ledger._owner_stamp()
    assert errors == ["queued text for telegram:chat for redelivery once the live adapter reconnects"]
    assert "could not queue" not in caplog.text
