"""API durable admission contracts: failed first ACK and explicit memory limits.

Only local aiohttp consumers, disposable SQLite, and mock agents are exercised.
HTTP 500 at the response seam is not physical network-loss or cross-runtime proof.
"""

import asyncio
import threading
from unittest.mock import MagicMock, patch

from aiohttp.test_utils import TestClient, TestServer
import pytest

from tests.gateway.test_api_server_runs import (
    _create_runs_app,
    _make_adapter,
    _use_idempotency_db,
)


class TestAPIRunDurableAcceptance:
    @pytest.mark.asyncio
    async def test_ack_failure_after_dispatch_replays_after_adapter_restart(
        self, tmp_path
    ):
        """A lost first 202 must not admit a second execution after restart.

        The response seam fails only after the real reservation and task dispatch.
        A fresh adapter backed by the same SQLite file must replay that reservation
        without constructing or dispatching a second agent.
        """
        path = tmp_path / "idem.db"
        first_adapter = _make_adapter()
        _use_idempotency_db(first_adapter, path)
        first_app = _create_runs_app(first_adapter)
        started = threading.Event()
        calls = 0

        def run(**kwargs):
            nonlocal calls
            calls += 1
            started.set()
            return {"final_response": "done"}

        agent = MagicMock()
        agent.run_conversation.side_effect = run
        agent.session_prompt_tokens = agent.session_completion_tokens = (
            agent.session_total_tokens
        ) = 0
        headers = {"Idempotency-Key": "lost-first-ack"}
        body = {"input": "durable dispatch"}

        async with TestClient(TestServer(first_app)) as cli:
            with (
                patch.object(first_adapter, "_create_agent", return_value=agent) as create,
                patch(
                    "gateway.platforms.api_server_runs._accepted_response",
                    side_effect=RuntimeError("first 202 acknowledgement lost"),
                ),
            ):
                failed = await cli.post("/v1/runs", json=body, headers=headers)
                assert failed.status == 500
                assert await asyncio.to_thread(started.wait, 5)
            tasks = tuple(first_adapter._active_run_tasks.values())
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            assert create.call_count == 1
        assert len(first_adapter._run_idempotency_ids) == 1
        original_run_id = next(iter(first_adapter._run_idempotency_ids))
        first_adapter._run_idempotency_store.close()

        restarted = _make_adapter()
        _use_idempotency_db(restarted, path)
        restarted_app = _create_runs_app(restarted)
        try:
            async with TestClient(TestServer(restarted_app)) as cli:
                with patch.object(restarted, "_create_agent") as create:
                    replay = await cli.post("/v1/runs", json=body, headers=headers)
                replay_body = await replay.json()
        finally:
            restarted._run_idempotency_store.close()

        assert replay.status == 202
        assert replay_body["replayed"] is True
        assert replay_body["run_id"] == original_run_id
        assert replay.headers["Idempotency-Replayed"] == "true"
        assert create.call_count == 0
        assert calls == 1

    def test_memory_store_declares_no_cross_restart_replay(self):
        """`:memory:` is deliberately process-local and cannot certify recovery."""
        from gateway.platforms.api_server_run_idempotency import RunIdempotencyStore

        first = RunIdempotencyStore(":memory:")
        assert first.durable is False
        assert (
            first.reserve(
                "scope", "memory-key", "fingerprint", "run_first",
                {"run_id": "run_first", "status": "queued"},
            )[0]
            == "created"
        )
        first.close()

        restarted = RunIdempotencyStore(":memory:")
        assert restarted.durable is False
        outcome, record = restarted.reserve(
            "scope", "memory-key", "fingerprint", "run_second",
            {"run_id": "run_second", "status": "queued"},
        )
        restarted.close()

        assert outcome == "created"
        assert record["run_id"] == "run_second"
