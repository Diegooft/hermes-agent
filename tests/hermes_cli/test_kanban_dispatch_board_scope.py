"""A profile allowlist must not authorize unrelated boards."""
import pytest
from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_db_dispatch as kbd

@pytest.mark.parametrize("value", ["[growth]", "[]", "null", "123"])
def test_outside_board_never_mutates_or_fires_intake(tmp_path, monkeypatch, all_assignees_spawnable, value):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path/"config.yaml").write_text(f"kanban:\n  dispatch_boards: {value}\n")
    hooks=[]
    monkeypatch.setattr(kb, "_fire_dispatch_tick_hook", lambda *a, **k: hooks.append(k))
    with kbc.connect(board="unrelated") as conn:
        tid=kb.create_task(conn,title="Other board",assignee="default")
        before=conn.total_changes
        res=kbd.dispatch_once(conn,board="unrelated",spawn_fn=lambda *a: 4242)
        assert not res.spawned
        assert conn.total_changes == before
        assert kb.get_task(conn,tid).status == "ready"
    assert hooks == []

def test_allowed_board_keeps_native_dispatch(tmp_path, monkeypatch, all_assignees_spawnable):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path/"config.yaml").write_text("kanban:\n  dispatch_boards: [growth]\n")
    with kbc.connect(board="growth") as conn:
        tid=kb.create_task(conn,title="Growth",assignee="default")
        result=kbd.dispatch_once(conn,board="growth",dry_run=True)
    assert [t for t, _, _ in result.spawned] == [tid]


@pytest.mark.parametrize("field,value", [("provider_override","claude-code-cli"),("model_override","paid-model"),("reasoning_effort","high")])
def test_existing_ready_card_cannot_override_execution_route(tmp_path, monkeypatch, all_assignees_spawnable, field, value):
    monkeypatch.setenv("HERMES_HOME",str(tmp_path))
    (tmp_path/"config.yaml").write_text("kanban:\n  dispatch_boards: [growth]\n  dispatch_no_task_overrides: true\n")
    with kbc.connect(board="growth") as conn:
        tid=kb.create_task(conn,title="SPEC",assignee="default")
        with kbc.write_txn(conn):
            conn.execute(f"UPDATE tasks SET {field}=? WHERE id=?",(value,tid))
        result=kbd.dispatch_once(conn,board="growth",dry_run=True)
        assert not result.spawned
        assert tid in result.skipped_nonspawnable
        assert kb.get_task(conn,tid).status=="ready"
