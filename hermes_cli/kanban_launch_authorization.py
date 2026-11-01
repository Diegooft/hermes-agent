"""Revalidate a guarded native dispatch in the child before its first turn.

The local task database and configured Python plugins are trusted control-plane
code. A receipt is a binding to a particular claim and policy, not a new grant.
It cannot grant Synapse authority or revoke tools in an already running turn.
"""
from contextlib import closing
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':'), default=str).encode('utf-8')).hexdigest()


def _task_signature(task):
    value = asdict(task)
    # Heartbeats, PID publication and claim renewal are not contract changes.
    for key in ('worker_pid', 'last_heartbeat_at', 'claim_expires'):
        value.pop(key, None)
    return _hash(value)


def _policy():
    from hermes_cli.config_effective import load_user_config_effective
    config = load_user_config_effective(fail_closed=True) or {}
    kanban = config.get('kanban', {})
    if not isinstance(kanban, dict) or not isinstance(config.get('plugins', {}), dict):
        raise ValueError('invalid dispatch policy')
    guards = kanban.get('dispatch_guards', [])
    if not isinstance(guards, list):
        raise ValueError('invalid dispatch policy')
    # Only the digest is persisted, never the config or callback exception text.
    return guards, _hash({'kanban': kanban, 'plugins': config.get('plugins', {})})


def record_launch_authorization(conn, task, *, board):
    """Persist the dispatch binding under the same claim CAS; no legacy write."""
    from hermes_cli import kanban_db as kb
    from hermes_constants import get_hermes_home
    guards, signature = _policy()
    if not guards:
        return True
    with kb.write_txn(conn):
        current = kb.get_task(conn, task.id)
        if current != task or task.status != 'running':
            return False
        kb._append_event(conn, task.id, 'dispatch_authorized', {
            'version': 1, 'policy_home': str(get_hermes_home()),
            'board': kb._normalize_board_slug(board) or kb.get_current_board(),
            'task_sha256': _task_signature(task), 'policy_sha256': signature,
        }, run_id=task.current_run_id)
    return True


def _connection(mode):
    raw = os.environ.get('HERMES_KANBAN_DB')
    if not raw:
        raise ValueError('worker database pin missing')
    path = Path(raw).expanduser().resolve()
    conn = sqlite3.connect(path.as_uri() + '?mode=' + mode, uri=True, timeout=5, isolation_level=None)
    conn.row_factory = sqlite3.Row
    if mode == 'ro':
        conn.execute('PRAGMA query_only=ON')
    return conn


def _owned_task(conn):
    from hermes_cli import kanban_db as kb
    task = kb.get_task(conn, os.environ['HERMES_KANBAN_TASK'])
    if (task is None or task.status != 'running'
            or str(task.current_run_id) != os.environ.get('HERMES_KANBAN_RUN_ID')
            or not task.claim_lock or task.claim_lock != os.environ.get('HERMES_KANBAN_CLAIM_LOCK')
            or not task.claim_expires or task.claim_expires <= time.time()):
        return None
    return task


def worker_launch_allowed():
    """Read-only check in the child. Non-kanban CLI is unaffected.

    The parent cannot authorize against stale cached plugin/settings state:
    the child independently loads/checks required guards in the dispatch home.
    This is a launch boundary, not a sandbox for installed Python or an atomic
    transaction with an external source changing after the check completes.
    """
    if not os.environ.get('HERMES_KANBAN_TASK'):
        return True
    from hermes_cli.kanban_dispatch_guards import dispatch_guards_allowed
    from hermes_cli.kanban_dispatch_scope import dispatch_board_allowed, dispatch_task_route_allowed
    from hermes_cli import kanban_db_dispatch as dispatch
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    try:
        with closing(_connection('ro')) as conn:
            task = _owned_task(conn)
            if task is None:
                return False
            row = conn.execute(
                "SELECT payload FROM task_events WHERE task_id = ? AND run_id = ? "
                "AND kind = 'dispatch_authorized' ORDER BY id DESC LIMIT 1",
                (task.id, task.current_run_id),
            ).fetchone()
            if row is None:
                # Older unguarded workers retain their existing startup contract.
                guards, _ = _policy()
                return not guards
            receipt = json.loads(row['payload'])
            board = os.environ.get('HERMES_KANBAN_BOARD', 'default')
            if (receipt.get('version') != 1 or receipt.get('board') != board
                    or not isinstance(receipt.get('policy_home'), str)
                    or receipt.get('task_sha256') != _task_signature(task)):
                return False
            token = set_hermes_home_override(receipt['policy_home'])
            try:
                guards, signature = _policy()
                profile_exists = dispatch._profile_exists_fn()
                if (not guards or signature != receipt.get('policy_sha256')
                        or not dispatch_board_allowed(board)
                        or (profile_exists is not None and not profile_exists(task.assignee))
                        or not dispatch_task_route_allowed(task)
                        or not dispatch_guards_allowed(task, phase='spawn', board=board)):
                    return False
                _, after_signature = _policy()
                after = _owned_task(conn)
                return (after is not None and _task_signature(after) == receipt['task_sha256']
                        and after_signature == signature)
            finally:
                reset_hermes_home_override(token)
    except (Exception, SystemExit):
        return False


def record_worker_authorization_wait():
    """Close only this child's claim through the native lifecycle, without retries."""
    from hermes_cli import kanban_db as kb
    try:
        with closing(_connection('rw')) as conn:
            task = _owned_task(conn)
            if task is not None:
                return kb.block_task(conn, task.id, kind='capability',
                    reason='DISPATCH_AUTHORIZATION: CHILD_REVALIDATION_REFUSED',
                    expected_run_id=task.current_run_id, expected_claim_lock=task.claim_lock)
    except (Exception, SystemExit):
        pass
    return False
