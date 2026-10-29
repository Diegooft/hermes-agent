from hermes_cli import kanban_db as kb, kanban_db_connect as kbc

def test_unblock_refuses_changed_event_history(tmp_path):
    with kbc.connect(db_path=tmp_path/"db") as conn:
        tid=kb.create_task(conn,title="SPEC",assignee="default")
        kb.block_task(conn,tid,kind="needs_input",reason="Owner approval")
        event=kb.list_events(conn,tid)[-1].id
        kb.edit_task(conn,tid,body="Changed scope")
        assert not kb.unblock_task(conn,tid,expected_event_id=event)
        assert kb.get_task(conn,tid).status=="blocked"

def test_unblock_restores_triage_without_authorizing_execution(tmp_path):
    with kbc.connect(db_path=tmp_path/"db") as conn:
        tid=kb.create_task(conn,title="SPEC",assignee="default",triage=True)
        with kbc.write_txn(conn):
            conn.execute("UPDATE tasks SET status='blocked',block_kind='needs_input' WHERE id=?",(tid,))
            kb._append_event(conn,tid,"blocked",{"source_status":"triage","kind":"needs_input"})
        assert kb.unblock_task(conn,tid)
        assert kb.get_task(conn,tid).status=="triage"
