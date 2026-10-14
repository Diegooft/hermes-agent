"""Offline C14.2 companion: child self-exit, no parent signal or provider.

This fixture measures the real Hermes delivery-ledger checkpoints through the
production startup and authorization mixins against a local fsync journal.  It
is component evidence only: it does not instantiate the GatewayRunner facade,
run the native startup sequence, or measure the original Cell5
SIGKILL path.  An ``attempting`` row whose journaled effect precedes the crash
is an explicit at-least-once counterexample: replay adds a visibly marked
second effect; that outcome is never treated as exactly-once acceptance.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.platforms('linux')

from gateway.delivery_ledger import RECOVERED_MARKER


_CWD = Path.cwd().resolve()
REPO_ROOT = _CWD if (_CWD / "gateway").is_dir() else Path(__file__).resolve().parents[3]
CHILD_DEADLINE_SECONDS = 30
CONTENT = "reply"
RESULT_PREFIX = "C14_RESULT="


CHILD = r'''
import asyncio
import json
import os
import threading
import time
from pathlib import Path

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent
from gateway.authz_mixin import GatewayAuthorizationMixin
from gateway.run_startup import GatewayStartupMixin
from gateway.session import AsyncSessionStore, SessionSource, SessionStore

ROOT = Path(os.environ["C14_ROOT"])
JOURNAL = ROOT / "journal.jsonl"
IDS = ROOT / "ids.json"
ROLE = os.environ["C14_ROLE"]
CONTENT = "reply"
SESSION_KEY = "agent:main:telegram:dm:c14-offline"
CHAT_ID = "4242"


class StartupReplayComponent(GatewayAuthorizationMixin, GatewayStartupMixin):
    """Test-only receiver for the unmodified production recovery mixins."""


assert StartupReplayComponent.__mro__ == (
    StartupReplayComponent,
    GatewayAuthorizationMixin,
    GatewayStartupMixin,
    object,
)
for _target_name in (
    "_clear_resume_pending_for_claimed_obligations",
    "_claim_pending_obligations",
    "_schedule_flood_redelivery",
    "_arm_flood_timers_for_waiting_rows",
    "_redeliver_claimed_obligations",
    "_obligation_adapter",
):
    assert getattr(StartupReplayComponent, _target_name) is getattr(
        GatewayStartupMixin, _target_name
    )


def _watchdog():
    # Parent never signals this process.  A fixture hang is bounded by a
    # child-owned terminal action, so cleanup cannot target the pytest parent
    # or an unrelated process.
    time.sleep(20)
    os._exit(124)


threading.Thread(target=_watchdog, name="c14-child-watchdog", daemon=True).start()


def _append_effect(content):
    line = (json.dumps({"role": ROLE, "content": content}, sort_keys=True) + "\n").encode()
    fd = os.open(str(JOURNAL), os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    try:
        os.write(fd, line)
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_ids(oid, scenario):
    payload = json.dumps({"obligation_id": oid, "scenario": scenario}, sort_keys=True).encode()
    fd = os.open(str(IDS), os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)


class JournalAdapter(BasePlatformAdapter):
    def __init__(self, *, exit_after_effect=False):
        super().__init__(PlatformConfig(enabled=True, token="fixture"), Platform.TELEGRAM)
        self.exit_after_effect = exit_after_effect

    async def connect(self, **kwargs):
        raise AssertionError("connect is forbidden in the offline fixture")

    async def disconnect(self):
        return None

    async def get_chat_info(self, chat_id):
        return {"id": chat_id}

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        _append_effect(content)
        if self.exit_after_effect:
            os._exit(137)
        return SendResult(success=True, message_id="fixture")


def _event():
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id=CHAT_ID,
        chat_type="dm",
        user_id="u",
    )
    return MessageEvent(text="question", source=source, message_id="m-c14")


async def _seed_pending():
    import gateway.delivery_ledger as ledger

    oid = ledger.compute_obligation_id(SESSION_KEY, "m-c14", CONTENT)
    decision = ledger.record_obligation(
        obligation_id=oid,
        session_key=SESSION_KEY,
        platform="telegram",
        chat_id=CHAT_ID,
        thread_id=None,
        content=CONTENT,
        adapter_profile="default",
    )
    assert decision.obligation_id == oid
    assert decision.state == "pending"
    assert decision.send_allowed is True
    _write_ids(oid, "pending-before-effect")
    os._exit(137)


async def _seed_attempting_after_effect():
    adapter = JournalAdapter(exit_after_effect=True)
    event = _event()
    oid = await adapter._record_delivery_obligation(event, SESSION_KEY, CONTENT, adapter, False)
    assert isinstance(oid, str) and oid
    _write_ids(oid, "attempting-after-effect")
    await adapter.send(CHAT_ID, CONTENT)
    raise AssertionError("self-exit after journal fsync did not occur")


async def _seed_delivered_control():
    adapter = JournalAdapter()
    event = _event()
    oid = await adapter._record_delivery_obligation(event, SESSION_KEY, CONTENT, adapter, False)
    assert isinstance(oid, str) and oid
    _write_ids(oid, "delivered-control")
    result = await adapter.send(CHAT_ID, CONTENT)
    await adapter._finalize_delivery_obligation(oid, result, event, adapter)
    os._exit(137)


async def _replay():
    adapter = JournalAdapter()
    runner = object.__new__(StartupReplayComponent)
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._profile_adapters = {}
    runner.session_store = SessionStore(
        Path(os.environ["HERMES_HOME"]) / "sessions", GatewayConfig()
    )
    runner.async_session_store = AsyncSessionStore(runner.session_store)
    claimed = await runner._claim_pending_obligations()
    assert all(row.get("profile") == "default" for row in claimed)
    delivered = await runner._redeliver_claimed_obligations(claimed)
    print(
        "C14_RESULT="
        + json.dumps(
            {
                "claimed": [row["obligation_id"] for row in claimed],
                "delivered": delivered,
            },
            sort_keys=True,
        ),
        flush=True,
    )


if ROLE == "seed-pending":
    asyncio.run(_seed_pending())
elif ROLE == "seed-attempting":
    asyncio.run(_seed_attempting_after_effect())
elif ROLE == "seed-delivered":
    asyncio.run(_seed_delivered_control())
elif ROLE == "replay":
    asyncio.run(_replay())
else:
    raise AssertionError(f"unknown role: {ROLE}")
'''


def _fixture_env(root: Path, role: str) -> dict[str, str]:
    home = root / "home"
    hermes_home = root / "isolated-hermes"
    temp_dir = root / "tmp"
    local_app_data = root / "local-app-data"
    for path in (home, hermes_home, temp_dir, local_app_data):
        path.mkdir(parents=True, exist_ok=True)
    (hermes_home / "config.yaml").write_text(
        "gateway:\n  delivery_ledger: true\n", encoding="utf-8"
    )

    # Start from an allowlist.  The isolation marker keeps the real guard
    # armed; HERMES_HOME is deliberately distinct from every platform-default
    # Hermes root, so no bypass is required.
    env = {
        key: os.environ[key]
        for key in ("SYSTEMROOT", "WINDIR")
        if os.environ.get(key)
    }
    env.update(
        {
            "PYTHONPATH": str(REPO_ROOT),
            "PYTHONUNBUFFERED": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "HOME": str(home),
            "USERPROFILE": str(home),
            "LOCALAPPDATA": str(local_app_data),
            "XDG_STATE_HOME": str(root / "xdg-state"),
            "HERMES_HOME": str(hermes_home),
            "HERMES_TEST_ISOLATION": str(hermes_home),
            "HERMES_DISABLE_LAZY_INSTALLS": "1",
            "TMPDIR": str(temp_dir),
            "TMP": str(temp_dir),
            "TEMP": str(temp_dir),
            "C14_ROOT": str(root),
            "C14_ROLE": role,
        }
    )
    return env


def _run_child(root: Path, role: str, expected_rc: int) -> tuple[str, str]:
    child = subprocess.Popen(
        [sys.executable, "-c", CHILD],
        cwd=str(REPO_ROOT),
        env=_fixture_env(root, role),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        stdout, stderr = child.communicate(timeout=CHILD_DEADLINE_SECONDS)
    finally:
        # No kill/terminate call is permitted.  If communicate itself fails,
        # the child-owned watchdog is the only actor that ends the child.
        if child.poll() is None:
            child.wait(timeout=CHILD_DEADLINE_SECONDS)
    assert child.returncode == expected_rc, (
        f"{role} returned {child.returncode}, expected {expected_rc}: {stderr[-2000:]}"
    )
    return stdout, stderr


def _result(stdout: str) -> dict:
    rows = [line[len(RESULT_PREFIX) :] for line in stdout.splitlines() if line.startswith(RESULT_PREFIX)]
    assert len(rows) == 1, f"expected one replay result, got {rows!r}"
    return json.loads(rows[0])


def _journal(root: Path) -> list[str]:
    path = root / "journal.jsonl"
    if not path.exists():
        return []
    return [json.loads(line)["content"] for line in path.read_text(encoding="utf-8").splitlines()]


def _ledger(root: Path) -> dict[str, tuple[str, int]]:
    db_path = root / "isolated-hermes" / "state.db"
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return {
            oid: (state, attempts)
            for oid, state, attempts in connection.execute(
                "SELECT obligation_id, state, attempts FROM delivery_obligations"
            )
        }
    finally:
        connection.close()


def _ids(root: Path) -> dict:
    return json.loads((root / "ids.json").read_text(encoding="utf-8"))


def _replay_twice_and_assert_noop(root: Path, oid: str, expected_first: list[str]) -> dict:
    first_stdout, _ = _run_child(root, "replay", 0)
    first = _result(first_stdout)
    assert first == {"claimed": [oid], "delivered": 1}
    assert _journal(root) == expected_first
    first_ledger = _ledger(root)
    assert set(first_ledger) == {oid}
    assert first_ledger[oid][0] == "delivered"

    second_stdout, _ = _run_child(root, "replay", 0)
    second = _result(second_stdout)
    assert second == {"claimed": [], "delivered": 0}
    assert _journal(root) == expected_first
    assert _ledger(root) == first_ledger
    return first


def test_confirmed_unsent_pending_replays_plain_once_then_is_noop(tmp_path: Path):
    _run_child(tmp_path, "seed-pending", 137)
    ids = _ids(tmp_path)
    assert ids["scenario"] == "pending-before-effect"
    assert _journal(tmp_path) == []
    oid = ids["obligation_id"]
    assert _ledger(tmp_path) == {oid: ("pending", 0)}
    _replay_twice_and_assert_noop(tmp_path, oid, [CONTENT])


def test_effect_before_receipt_is_marked_at_least_once_counterexample(tmp_path: Path):
    _run_child(tmp_path, "seed-attempting", 137)
    ids = _ids(tmp_path)
    assert ids["scenario"] == "attempting-after-effect"
    assert _journal(tmp_path) == [CONTENT]
    oid = ids["obligation_id"]
    assert _ledger(tmp_path) == {oid: ("attempting", 0)}
    expected = [CONTENT, RECOVERED_MARKER + CONTENT]
    _replay_twice_and_assert_noop(tmp_path, oid, expected)
    assert len(expected) == 2  # explicit counterexample: two durable effects, second visibly marked


def test_delivered_control_never_replays_after_abrupt_child_exit(tmp_path: Path):
    _run_child(tmp_path, "seed-delivered", 137)
    ids = _ids(tmp_path)
    oid = ids["obligation_id"]
    assert ids["scenario"] == "delivered-control"
    assert _journal(tmp_path) == [CONTENT]
    before = _ledger(tmp_path)
    assert before == {oid: ("delivered", 0)}
    for _ in range(2):
        stdout, _ = _run_child(tmp_path, "replay", 0)
        assert _result(stdout) == {"claimed": [], "delivered": 0}
    assert _journal(tmp_path) == [CONTENT]
    assert _ledger(tmp_path) == before
