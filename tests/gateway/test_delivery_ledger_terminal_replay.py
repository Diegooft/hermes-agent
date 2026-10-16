"""Terminal delivery obligations never reopen into another platform send."""

import asyncio
import threading
from types import SimpleNamespace

import pytest

from gateway import delivery_ledger as ledger
from gateway.platforms.base import BasePlatformAdapter, SendResult


class _Transport(BasePlatformAdapter):
    def __init__(self):
        self.sends = 0
        self.typed_command_prefix = "!"

    @property
    def name(self):
        return "fixture"

    @property
    def platform(self):
        return SimpleNamespace(value="telegram")

    async def connect(self):
        pass

    async def disconnect(self):
        pass

    async def get_chat_info(self, chat_id):
        return {}

    async def send(self, **kwargs):
        self.sends += 1
        return SendResult(success=True, message_id=str(self.sends))


@pytest.fixture
def isolated_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "_db_path", lambda: tmp_path / "state.db")


@pytest.mark.parametrize(("state", "expected_success", "expected_error"), [
    ("delivered", True, None), ("abandoned", False, "DELIVERY_ABANDONED"),
])
def test_terminal_obligation_suppresses_replay_and_releases_marker(
    isolated_ledger, state, expected_success, expected_error,
):
    adapter = _Transport()
    released = []

    async def clear_marker(event):
        released.append(event)

    adapter.gateway_runner = SimpleNamespace(_clear_durable_active_turn=clear_marker)
    source = SimpleNamespace(platform=SimpleNamespace(value="telegram"), chat_id="chat", thread_id=None)
    event = SimpleNamespace(text="question", message_id="request", ledger_message_id=None, source=source,
                            _turn_marker_handoff=True, _gateway_active_turn_token="token")
    oid = ledger.compute_obligation_id("session", "request", "answer")
    ledger.record_obligation(obligation_id=oid, session_key="session", platform="telegram", chat_id="chat",
                             thread_id=None, content="answer")
    if state == "delivered":
        ledger.mark_delivered(oid)
    else:
        with ledger._connect() as conn:
            conn.execute("UPDATE delivery_obligations SET state='abandoned', attempts=? WHERE obligation_id=?",
                         (ledger.MAX_ATTEMPTS, oid))

    result, _ = asyncio.run(adapter.send_final_ledgered(event, "session", "answer", {}, reply_to=None))

    assert result.success is expected_success
    assert result.error == expected_error
    assert adapter.sends == 0
    assert released == [event]
    with ledger._connect() as conn:
        assert conn.execute("SELECT state, attempts FROM delivery_obligations WHERE obligation_id=?", (oid,)).fetchone() == (
            state, 0 if state == "delivered" else ledger.MAX_ATTEMPTS)


def test_first_send_and_failed_retry_remain_sendable(isolated_ledger):
    adapter = _Transport()
    source = SimpleNamespace(platform=SimpleNamespace(value="telegram"), chat_id="chat", thread_id=None)
    event = SimpleNamespace(text="question", message_id="request", ledger_message_id=None, source=source)

    first, _ = asyncio.run(adapter.send_final_ledgered(event, "session", "answer", {}, reply_to=None))
    oid = ledger.compute_obligation_id("retry", "request", "answer")
    ledger.record_obligation(obligation_id=oid, session_key="retry", platform="telegram", chat_id="chat",
                             thread_id=None, content="answer")
    ledger.mark_failed(oid, "temporary")
    retry = ledger.record_final_obligation(session_key="retry", message_ref="request", platform="telegram",
                                           chat_id="chat", thread_id=None, content="answer")

    assert first.success and adapter.sends == 1
    assert retry == oid
    with ledger._connect() as conn:
        assert conn.execute("SELECT state FROM delivery_obligations WHERE obligation_id=?", (oid,)).fetchone() == ("attempting",)


def test_replaying_same_final_input_sends_once_through_real_caller(isolated_ledger):
    adapter = _Transport()
    source = SimpleNamespace(platform=SimpleNamespace(value="telegram"), chat_id="chat", thread_id=None)
    event = SimpleNamespace(text="question", message_id="request", ledger_message_id=None, source=source)
    args = (event, "session", "answer", {})

    first, first_adapter = asyncio.run(adapter.send_final_ledgered(*args, reply_to=None))
    replay, replay_adapter = asyncio.run(adapter.send_final_ledgered(*args, reply_to=None))

    oid = ledger.compute_obligation_id("session", "request", "answer")
    assert first.success
    assert first_adapter is adapter
    assert replay.success
    assert replay.raw_response == {"ledger": "already_delivered"}
    assert replay_adapter is adapter
    assert adapter.sends == 1
    with ledger._connect() as conn:
        assert conn.execute(
            "SELECT state FROM delivery_obligations WHERE obligation_id=?", (oid,)
        ).fetchone() == ("delivered",)


@pytest.mark.parametrize("terminal", ["delivered", "abandoned"])
def test_terminalized_after_observation_never_reaches_transport(isolated_ledger, monkeypatch, terminal):
    adapter = _Transport()
    source = SimpleNamespace(platform=SimpleNamespace(value="telegram"), chat_id="chat", thread_id=None)
    event = SimpleNamespace(text="question", message_id="request", ledger_message_id=None, source=source)
    oid = ledger.compute_obligation_id("session", "request", "answer")
    ledger.record_obligation(obligation_id=oid, session_key="session", platform="telegram", chat_id="chat",
                             thread_id=None, content="answer")
    ledger.mark_failed(oid, "temporary")
    with ledger._connect() as conn:
        conn.execute("UPDATE delivery_obligations SET owner_pid=?, owner_started_at=? WHERE obligation_id=?",
                     (999999999, 1, oid))
    observed, continue_claim = threading.Event(), threading.Event()

    def dead_owner_after_barrier(*_):
        observed.set()
        assert continue_claim.wait(2)
        return False

    monkeypatch.setattr(ledger, "_owner_alive", dead_owner_after_barrier)
    result = []
    worker = threading.Thread(target=lambda: result.append(asyncio.run(
        adapter.send_final_ledgered(event, "session", "answer", {}, reply_to=None))))
    worker.start()
    assert observed.wait(2)
    with ledger._connect() as conn:
        conn.execute("UPDATE delivery_obligations SET state=? WHERE obligation_id=?", (terminal, oid))
    continue_claim.set()
    worker.join(2)

    sent, _ = result[0]
    assert sent.success is (terminal == "delivered")
    assert sent.raw_response == {"ledger": "already_delivered" if terminal == "delivered" else terminal}
    assert adapter.sends == 0
    with ledger._connect() as conn:
        assert conn.execute("SELECT state FROM delivery_obligations WHERE obligation_id=?", (oid,)).fetchone() == (terminal,)


@pytest.mark.parametrize("state", ["pending", "failed"])
def test_dead_owner_claim_blocks_sweep_while_transport_is_live(isolated_ledger, state):
    sent, release = threading.Event(), threading.Event()

    class BlockingTransport(_Transport):
        async def send(self, **kwargs):
            self.sends += 1
            sent.set()
            assert release.wait(2)
            return SendResult(success=True, message_id="sent")

    adapter = BlockingTransport()
    source = SimpleNamespace(platform=SimpleNamespace(value="telegram"), chat_id="chat", thread_id=None)
    event = SimpleNamespace(text="question", message_id="request", ledger_message_id=None, source=source)
    oid = ledger.compute_obligation_id("session", "request", "answer")
    ledger.record_obligation(obligation_id=oid, session_key="session", platform="telegram", chat_id="chat",
                             thread_id=None, content="answer")
    if state == "failed":
        ledger.mark_failed(oid, "temporary")
    with ledger._connect() as conn:
        conn.execute("UPDATE delivery_obligations SET owner_pid=?, owner_started_at=? WHERE obligation_id=?",
                     (999999999, 1, oid))
    worker = threading.Thread(target=lambda: asyncio.run(
        adapter.send_final_ledgered(event, "session", "answer", {}, reply_to=None)))
    worker.start()
    assert sent.wait(2)

    assert ledger.sweep_recoverable(deliverable_platforms={"telegram"}) == []
    with ledger._connect() as conn:
        owner = conn.execute("SELECT owner_pid, owner_started_at FROM delivery_obligations WHERE obligation_id=?", (oid,)).fetchone()
    assert owner == ledger._owner_stamp()
    release.set()
    worker.join(2)
