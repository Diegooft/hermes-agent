"""Launch-boundary checks run without starting an agent or querying a model."""
import os
import pytest
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from tests.hermes_cli.test_kanban_dispatch_guards import home, install, configure, ready


def child_env(monkeypatch, task):
    monkeypatch.setenv('HERMES_KANBAN_TASK', task.id)
    monkeypatch.setenv('HERMES_KANBAN_DB', str(kb.kanban_db_path()))
    monkeypatch.setenv('HERMES_KANBAN_BOARD', 'default')
    monkeypatch.setenv('HERMES_KANBAN_RUN_ID', str(task.current_run_id))
    monkeypatch.setenv('HERMES_KANBAN_CLAIM_LOCK', task.claim_lock)


@pytest.mark.parametrize('change', ['body', 'config', 'disabled', 'claim', 'missing_receipt'])
def test_child_refuses_revocation_between_dispatch_and_first_turn(home, monkeypatch, change):
    from hermes_cli.kanban_launch_authorization import worker_launch_allowed
    install(home)
    observed = []
    with kbc.connect() as conn:
        tid = ready(conn)
        def spawn(task, *args):
            child_env(monkeypatch, task)
            if change == 'body':
                with kb.write_txn(conn):
                    conn.execute("UPDATE tasks SET body = 'changed after parent check' WHERE id = ?", (tid,))
            elif change == 'config':
                configure(home, '[]')
            elif change == 'disabled':
                with (home / 'config.yaml').open('a', encoding='utf-8') as f:
                    f.write('plugins:\n  disabled: [policy_probe]\n')
            elif change == 'claim':
                with kb.write_txn(conn):
                    conn.execute("UPDATE tasks SET claim_lock = 'other-run' WHERE id = ?", (tid,))
            else:
                with kb.write_txn(conn):
                    conn.execute("DELETE FROM task_events WHERE task_id = ? AND kind = 'dispatch_authorized'", (tid,))
            observed.append(worker_launch_allowed())
        kbd.dispatch_once(conn, spawn_fn=spawn, reconcile_orphans=False)
        assert observed == [False]


def test_child_revalidates_in_dispatch_home_even_when_worker_home_differs(home, monkeypatch, tmp_path):
    from hermes_cli.kanban_launch_authorization import worker_launch_allowed
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    install(home)
    other = tmp_path / 'worker-profile'
    other.mkdir()
    observed = []
    with kbc.connect() as conn:
        ready(conn)
        def spawn(task, *args):
            child_env(monkeypatch, task)
            token = set_hermes_home_override(other)
            try:
                assert worker_launch_allowed()
                configure(home, '[missing:contract]')
                observed.append(worker_launch_allowed())
            finally:
                reset_hermes_home_override(token)
        kbd.dispatch_once(conn, spawn_fn=spawn, reconcile_orphans=False)
        assert observed == [False]


def test_child_missing_db_refuses_without_creating_file(home, monkeypatch):
    from hermes_cli.kanban_launch_authorization import worker_launch_allowed
    missing = home / 'does-not-exist.db'
    monkeypatch.setenv('HERMES_KANBAN_TASK', 'untrusted-task')
    monkeypatch.setenv('HERMES_KANBAN_DB', str(missing))
    assert not worker_launch_allowed()
    assert not missing.exists()


def test_regular_cli_and_legacy_workers_remain_supported(home, monkeypatch):
    from hermes_cli.kanban_launch_authorization import worker_launch_allowed
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    assert worker_launch_allowed()
    with kbc.connect() as conn:
        task = kb.claim_task(conn, ready(conn))
        child_env(monkeypatch, task)
        assert worker_launch_allowed()


def test_single_query_refusal_exits_before_importing_cli_or_starting_model(home, monkeypatch):
    from hermes_cli import kanban_launch_authorization as auth
    from hermes_cli.cli_single_query import _run_single_query_mode
    monkeypatch.setattr(auth, 'worker_launch_allowed', lambda: False)
    with pytest.raises(SystemExit) as error:
        _run_single_query_mode(object(), 'must not run', None, True, True)
    assert error.value.code == 78


def test_child_refusal_closes_only_owned_run_without_spending_retries(home, monkeypatch):
    from hermes_cli.kanban_launch_authorization import record_worker_authorization_wait
    with kbc.connect() as conn:
        task = kb.claim_task(conn, ready(conn))
        child_env(monkeypatch, task)
        assert record_worker_authorization_wait()
        current = kb.get_task(conn, task.id)
        assert current.status == 'blocked' and current.block_kind == 'capability'
        assert current.consecutive_failures == 0 and current.claim_lock is None


@pytest.mark.parametrize('change', [False, True])
def test_independent_python_child_rechecks_native_receipt_before_first_turn(home, monkeypatch, change):
    import json
    import subprocess
    import sys
    install(home)
    observed = []
    with kbc.connect() as conn:
        tid = ready(conn)
        def spawn(task, *args):
            child_env(monkeypatch, task)
            if change:
                configure(home, '[]')
            code = ('import sys; sys.path[:0] = ' + repr([str(p) for p in sys.path]) +
                '; from hermes_cli.kanban_launch_authorization import worker_launch_allowed; '
                'raise SystemExit(0 if worker_launch_allowed() else 78)')
            child = subprocess.run([sys.executable, '-I', '-c', code], env=dict(os.environ),
                                   capture_output=True, text=True, timeout=30)
            observed.append(child.returncode)
        kbd.dispatch_once(conn, spawn_fn=spawn, reconcile_orphans=False)
        assert observed == [78 if change else 0]


@pytest.mark.parametrize('branch', ['interactive', 'quiet', 'chat'])
def test_first_turn_rechecks_after_startup_can_revoke(home, monkeypatch, branch):
    import cli
    from types import SimpleNamespace
    from hermes_cli import kanban_launch_authorization as auth
    from hermes_cli.cli_single_query import _run_single_query_mode
    policy = {'allowed': True}
    waits = []
    monkeypatch.setattr(auth, 'worker_launch_allowed', lambda: policy['allowed'])
    monkeypatch.setattr(auth, 'record_worker_authorization_wait', lambda: waits.append(True))
    monkeypatch.setattr(cli, '_should_seed_interactive', lambda *a: branch == 'interactive')
    def revoke():
        policy['allowed'] = False
    def images(q, i):
        if branch == 'interactive':
            revoke()
        return q, []
    monkeypatch.setattr(cli, '_collect_query_images', images)
    monkeypatch.setattr(cli, '_collect_kanban_task_images', lambda *a: [])
    monkeypatch.setattr(cli, '_route_single_query_images', lambda *a: 'query')
    monkeypatch.setattr(cli, '_finalize_single_query', lambda *a: None)
    monkeypatch.setattr(cli, '_configure_quiet_agent', lambda *a: None)
    monkeypatch.setattr(cli, '_run_quiet_single_query', lambda *a, **k: pytest.fail('revoked quiet turn'))
    def init(**kwargs):
        revoke()
        return True
    stub = SimpleNamespace(
        _claim_active_session=lambda *a, **k: True,
        _ensure_runtime_credentials=lambda: True,
        _resolve_turn_agent_config=lambda *a: {'signature': 'same', 'model': None, 'runtime': None},
        _active_agent_route_signature='same', _init_agent=init, agent=object(),
        _show_security_advisories=revoke, console=SimpleNamespace(print=lambda *a: None),
        run=lambda: pytest.fail('revoked interactive turn'),
        chat=lambda *a, **k: pytest.fail('revoked chat turn'))
    with pytest.raises(SystemExit) as error:
        _run_single_query_mode(stub, 'query', None, branch == 'quiet', True)
    assert error.value.code == 78
    assert waits == [True]
