"""Persist acceptance with the same ownership snapshot as the terminal write."""
from __future__ import annotations

import json

from hermes_cli.kanban_db_connect import write_txn
from hermes_cli.kanban_pr_acceptance import _PR, collect_acceptance


def _snapshot(conn, task_id):
    row = conn.execute("SELECT current_run_id, status, completion_contract FROM tasks WHERE id=?", (task_id,)).fetchone()
    return tuple(row) if row else None


def prepare_acceptance(conn, task_id, expected_run_id, metadata):
    snapshot = _snapshot(conn, task_id)
    if snapshot is None:
        return False
    run_id, status, contract = snapshot
    if not contract or contract == "local-only":
        return None
    if status not in {"running", "ready", "blocked", "review"} or (expected_run_id is not None and run_id != expected_run_id):
        return False
    published_pr = metadata.get("published_pr") if isinstance(metadata, dict) else None
    match = _PR.fullmatch(published_pr) if isinstance(published_pr, str) else None
    if contract == "github_pr":
        # Preserve the delivery mode; bind its publication in the existing durable event log.
        from hermes_cli.kanban_db import _append_event
        with write_txn(conn):
            if _snapshot(conn, task_id) != snapshot:
                return False
            bound = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='pr_publication' ORDER BY rowid LIMIT 1", (task_id,)).fetchone()
            if bound:
                try:
                    pinned = json.loads(bound[0])["pr_url"]
                except (ValueError, KeyError, TypeError):
                    return False
                if not isinstance(pinned, str) or not _PR.fullmatch(pinned) or (published_pr and published_pr != pinned):
                    return False
                published_pr = pinned
            elif match:
                _append_event(conn, task_id, "pr_publication", {"pr_url": published_pr, "completion_contract": contract}, run_id=run_id)
        assignee = conn.execute("SELECT assignee FROM tasks WHERE id=?", (task_id,)).fetchone()["assignee"]
        return snapshot, collect_acceptance(contract, published_pr, assignee=assignee)
    # Publication binds once. Retrying cannot replace the task's PR with a green sibling.
    if match and contract == match[1]:
        with write_txn(conn):
            if _snapshot(conn, task_id) != snapshot:
                return False
            conn.execute("UPDATE tasks SET completion_contract=? WHERE id=?", (published_pr, task_id))
        snapshot = (run_id, status, published_pr)
        contract = published_pr
    # The assignee profile's gh login owns the repo: acceptance must not run as
    # the ambient login of whichever process completes the card (#122689).
    assignee = conn.execute("SELECT assignee FROM tasks WHERE id=?", (task_id,)).fetchone()["assignee"]
    return snapshot, collect_acceptance(contract, published_pr, assignee=assignee)


def record_acceptance(conn, task_id, acceptance):
    """Called under complete_task's write_txn, before its terminal UPDATE."""
    from hermes_cli.kanban_db import _append_event
    snapshot, receipt = acceptance
    if _snapshot(conn, task_id) != snapshot:
        return False
    _append_event(conn, task_id, "pr_acceptance", receipt, run_id=snapshot[0])
    if not receipt["ok"]:
        detail = f"PR acceptance {receipt['classification']}: {receipt.get('detail', '')} {receipt['recovery']}"
        conn.execute("UPDATE tasks SET last_failure_error=? WHERE id=?", (detail, task_id))
    return receipt["ok"]
