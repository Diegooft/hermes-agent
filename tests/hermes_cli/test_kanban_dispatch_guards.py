"""Mandatory policy checks use temporary homes, real plugin discovery and native dispatch."""

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


def install(home, source="return True"):
    plugin = home / "plugins" / "policy_probe"
    plugin.mkdir(parents=True, exist_ok=True)
    (plugin / "plugin.yaml").write_text("name: policy_probe\nversion: '1.0'\n", encoding="utf-8")
    (plugin / "__init__.py").write_text(
        "def check(task, phase, **kw):\n    " + source.replace("\n", "\n    ") +
        "\ndef register(ctx):\n    ctx.register_kanban_dispatch_guard('contract', check)\n",
        encoding="utf-8",
    )
    configure(home)


def configure(home, guards="[policy_probe:contract]"):
    (home / "config.yaml").write_text(
        "plugins:\n  enabled: [policy_probe]\nkanban:\n  dispatch_guards: " + guards + "\n",
        encoding="utf-8",
    )


@pytest.fixture
def home(tmp_path, monkeypatch, all_assignees_spawnable):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_HOME", raising=False)
    kb.init_db()
    yield home
    from hermes_cli.plugins import get_plugin_manager
    get_plugin_manager().unload()


def ready(conn):
    return kb.create_task(conn, title="canonical execution", assignee="default", workspace_kind="scratch")


def test_missing_required_guard_prevents_claim_and_dry_run_mutation(home):
    configure(home)
    with kbc.connect() as conn:
        tid = ready(conn)
        before = [e.kind for e in kb.list_events(conn, tid)]
        result = kbd.dispatch_once(conn, dry_run=True)
        assert result.spawned == []
        assert any(item[0] == tid for item in result.authorization_denied)
        assert [e.kind for e in kb.list_events(conn, tid)] == before
        assert kb.get_task(conn, tid).claim_lock is None


@pytest.mark.parametrize("guards", ["null", "true", "contract", "[42]", "[policy_probe:contract, policy_probe:contract]"])
def test_malformed_config_denies(home, guards):
    configure(home, guards)
    with kbc.connect() as conn:
        ready(conn)
        assert kbd.dispatch_once(conn, dry_run=True).spawned == []


@pytest.mark.parametrize("source", ["return None", "return 'yes'", "return {'allow': True}", "raise RuntimeError('denied')"])
def test_only_literal_true_allows(home, source):
    install(home, source)
    with kbc.connect() as conn:
        ready(conn)
        assert kbd.dispatch_once(conn, dry_run=True).spawned == []


def test_legacy_no_guards_still_dispatches(home):
    with kbc.connect() as conn:
        tid = ready(conn)
        assert [s[0] for s in kbd.dispatch_once(conn, dry_run=True).spawned] == [tid]


def test_real_plugin_accepts_immutable_snapshot_and_unload_revokes(home):
    from hermes_cli.kanban_dispatch_guards import dispatch_guards_allowed
    from hermes_cli.plugins import get_plugin_manager
    install(home, "assert isinstance(task['skills'], tuple)\ntry:\n    task['title'] = 'rewritten'\nexcept TypeError:\n    return True\nreturn False")
    with kbc.connect() as conn:
        tid = ready(conn)
        task = kb.get_task(conn, tid)
        task.skills = ["proof"]
        assert dispatch_guards_allowed(task, phase="claim", board="default")
        get_plugin_manager().unload("policy_probe")
        assert not dispatch_guards_allowed(task, phase="claim", board="default")
        assert kb.get_task(conn, tid).title == "canonical execution"


def test_guard_timeout_is_bounded_and_denies(home, monkeypatch):
    import hermes_cli.kanban_dispatch_guards as guards
    install(home, "import time\ntime.sleep(0.15)\nreturn True")
    monkeypatch.setattr(guards, "_GUARD_TIMEOUT_SECONDS", 0.02)
    with kbc.connect() as conn:
        task = kb.get_task(conn, ready(conn))
        assert not guards.dispatch_guards_allowed(task, phase="claim", board="default")


def test_revocation_during_workspace_resolution_blocks_native_run(home, monkeypatch):
    install(home)
    real = kbd._kbw.resolve_workspace
    spawned = []
    def revoke(task, **kw):
        workspace = real(task, **kw)
        configure(home, "[missing:contract]")
        return workspace
    monkeypatch.setattr(kbd._kbw, "resolve_workspace", revoke)
    with kbc.connect() as conn:
        tid = ready(conn)
        result = kbd.dispatch_once(conn, spawn_fn=lambda *a: spawned.append(a), reconcile_orphans=False)
        task = kb.get_task(conn, tid)
        assert spawned == []
        assert tid in result.auto_blocked
        assert task.status == "blocked"
        assert task.claim_lock is None


def test_fresh_body_after_claim_is_verified(home, monkeypatch):
    install(home, "return phase == 'claim' or task['body'] != 'REVOKED'")
    real = kbd._kbw.resolve_workspace
    with kbc.connect() as conn:
        tid = ready(conn)
        def update(task, **kw):
            workspace = real(task, **kw)
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET body = 'REVOKED' WHERE id = ?", (tid,))
            return workspace
        monkeypatch.setattr(kbd._kbw, "resolve_workspace", update)
        result = kbd.dispatch_once(conn, spawn_fn=lambda *a: pytest.fail("stale task spawned"), reconcile_orphans=False)
        assert tid in result.auto_blocked


def test_real_discovery_profiles_a_b_a(tmp_path, monkeypatch):
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from hermes_cli.plugins import get_plugin_manager
    from hermes_cli.kanban_dispatch_guards import dispatch_guards_allowed
    from dataclasses import make_dataclass
    Task = make_dataclass("Task", [("id", str), ("title", str)])
    task = Task("id", "scope")
    homes = [tmp_path / "A", tmp_path / "B"]
    for home in homes:
        home.mkdir()
    install(homes[0])
    configure(homes[1])
    try:
        for home, expected in [(homes[0], True), (homes[1], False), (homes[0], True)]:
            token = set_hermes_home_override(home)
            try:
                assert dispatch_guards_allowed(task, phase="claim", board="default") is expected
            finally:
                reset_hermes_home_override(token)
    finally:
        for home in homes:
            token = set_hermes_home_override(home)
            try:
                get_plugin_manager().unload()
            finally:
                reset_hermes_home_override(token)


def test_required_guard_applies_to_review_lane(home):
    configure(home)
    with kbc.connect() as conn:
        tid = ready(conn)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (tid,))
        result = kbd.dispatch_once(conn, dry_run=True)
        assert result.spawned == []
        assert any(item[0] == tid for item in result.authorization_denied)


def test_registration_is_namespaced_collision_safe_and_disposable(home):
    from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
    manager = PluginManager(str(home))
    one = PluginContext(PluginManifest(name="one"), manager)
    two = PluginContext(PluginManifest(name="two"), manager)
    handle = one.register_kanban_dispatch_guard("contract", lambda: True)
    two.register_kanban_dispatch_guard("contract", lambda: False)
    with pytest.raises(ValueError):
        one.register_kanban_dispatch_guard("contract", lambda: True)
    with pytest.raises(ValueError):
        two.register_kanban_dispatch_guard("one:contract", lambda: True)
    handle.dispose()
    assert "one:contract" not in manager._kanban_dispatch_guards
    assert "two:contract" in manager._kanban_dispatch_guards
    manager.unload()
    assert manager._kanban_dispatch_guards == {}


@pytest.mark.parametrize("setting", ["dispatch_boards: []", "dispatch_profiles: []"])
def test_board_and_profile_revocation_before_spawn(home, monkeypatch, setting):
    install(home)
    real = kbd._kbw.resolve_workspace
    def revoke(task, **kw):
        workspace = real(task, **kw)
        with (home / "config.yaml").open("a", encoding="utf-8") as config:
            config.write("  " + setting + "\n")
        return workspace
    monkeypatch.setattr(kbd._kbw, "resolve_workspace", revoke)
    with kbc.connect() as conn:
        tid = ready(conn)
        result = kbd.dispatch_once(conn, spawn_fn=lambda *a: pytest.fail("revoked route spawned"), reconcile_orphans=False)
        assert tid in result.auto_blocked
        assert kb.get_task(conn, tid).claim_lock is None


def test_disabled_plugin_rejects_cached_registration(home):
    from hermes_cli.kanban_dispatch_guards import dispatch_guards_allowed
    install(home)
    with kbc.connect() as conn:
        task = kb.get_task(conn, ready(conn))
        assert dispatch_guards_allowed(task, phase="claim", board="default")
        with (home / "config.yaml").open("a", encoding="utf-8") as config:
            config.write("plugins:\n  disabled: [policy_probe]\n")
        assert not dispatch_guards_allowed(task, phase="claim", board="default")


def test_success_checks_both_native_boundaries(home):
    install(home, "return (phase == 'claim' and task['status'] == 'ready') or (phase == 'spawn' and task['status'] == 'running' and bool(task['workspace_path']))")
    with kbc.connect() as conn:
        tid = ready(conn)
        result = kbd.dispatch_once(conn, spawn_fn=lambda *a: None, reconcile_orphans=False)
        assert [s[0] for s in result.spawned] == [tid]


def test_policy_refusal_blocks_even_with_high_retry_budget(home, monkeypatch):
    install(home, "return phase == 'claim'")
    with kbc.connect() as conn:
        tid = ready(conn)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET max_retries = 99 WHERE id = ?", (tid,))
        result = kbd.dispatch_once(conn, spawn_fn=lambda *a: pytest.fail("denied"), reconcile_orphans=False)
        assert tid in result.auto_blocked
        assert kb.get_task(conn, tid).status == "blocked"


def test_policy_failure_cannot_release_another_runs_claim(home):
    with kbc.connect() as conn:
        tid = ready(conn)
        claimed = kb.claim_task(conn, tid)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET claim_lock = 'new-owner' WHERE id = ?", (tid,))
        assert not kb.block_task(conn, tid, reason="policy changed", kind="capability",
            expected_run_id=claimed.current_run_id, expected_claim_lock=claimed.claim_lock)
        task = kb.get_task(conn, tid)
        assert task.claim_lock == "new-owner"
        assert task.status == "running"
        assert task.consecutive_failures == 0


def test_denied_review_does_not_starve_ready_with_one_slot(home):
    install(home, "return task['title'] != 'denied review'")
    with kbc.connect() as conn:
        review = ready(conn)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'review', title = 'denied review' WHERE id = ?", (review,))
        allowed = ready(conn)
        result = kbd.dispatch_once(conn, dry_run=True, max_spawn=1)
        assert [item[0] for item in result.spawned] == [allowed]
        assert len(result.authorization_denied) == 1


@pytest.mark.parametrize("lane", ["ready", "review"])
def test_plugin_system_exit_denies_without_crashing_dispatch(home, lane):
    install(home, "raise SystemExit(2)")
    with kbc.connect() as conn:
        tid = ready(conn)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (lane, tid))
        result = kbd.dispatch_once(conn, dry_run=True)
        assert result.spawned == []
        assert result.authorization_denied == [(tid, 'claim', 'GUARD_REFUSED_OR_UNAVAILABLE')]


def test_policy_wait_preserves_worker_retry_budget(home):
    install(home, "return phase == 'claim'")
    with kbc.connect() as conn:
        tid = ready(conn)
        result = kbd.dispatch_once(conn, spawn_fn=lambda *a: pytest.fail('denied'), reconcile_orphans=False)
        task = kb.get_task(conn, tid)
        assert tid in result.auto_blocked
        assert task.status == 'blocked' and task.block_kind == 'capability'
        assert task.consecutive_failures == 0


def test_denial_notice_is_rate_limited_without_spending_retries(home):
    configure(home)
    with kbc.connect() as conn:
        tid = ready(conn)
        for _ in range(2):
            result = kbd.dispatch_once(conn, reconcile_orphans=False)
            assert result.spawned == []
        notices = [e for e in kb.list_events(conn, tid) if e.kind == 'dispatch_authorization_denied']
        assert len(notices) == 1
        assert kb.get_task(conn, tid).consecutive_failures == 0
        assert 'authorization_denied=1' in kbd.describe_suppression([result])


@pytest.mark.parametrize("lane", ["ready", "review"])
def test_callback_keyboard_interrupt_is_policy_refusal(home, lane):
    install(home, "raise KeyboardInterrupt('callback interrupt')")
    with kbc.connect() as conn:
        tid = ready(conn)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (lane, tid))
        result = kbd.dispatch_once(conn, dry_run=True)
        assert not result.spawned
        assert result.authorization_denied


def test_real_dispatcher_interrupt_is_not_swallowed(home, monkeypatch):
    from hermes_cli.plugins import get_plugin_manager
    install(home)
    manager = get_plugin_manager()
    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt()
    monkeypatch.setattr(manager, '_run_hook_callback_bounded', interrupted)
    with kbc.connect() as conn:
        ready(conn)
        with pytest.raises(KeyboardInterrupt):
            kbd.dispatch_once(conn, dry_run=True)


def test_fast_child_cannot_publish_pid_onto_successor_claim(home, monkeypatch):
    install(home)
    hooks = []
    monkeypatch.setattr(kbd._kb, '_fire_worker_spawned_hook', lambda *a, **kw: hooks.append(a))
    with kbc.connect() as conn:
        tid = ready(conn)
        successor = []
        def spawn(task, *args):
            assert kb.block_task(conn, tid, reason='child refused', kind='capability',
                expected_run_id=task.current_run_id, expected_claim_lock=task.claim_lock)
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
            successor.append(kb.claim_task(conn, tid))
            return 12345
        result = kbd.dispatch_once(conn, spawn_fn=spawn, reconcile_orphans=False)
        current = kb.get_task(conn, tid)
        assert current.current_run_id == successor[0].current_run_id
        assert current.worker_pid is None
        assert current.claim_lock == successor[0].claim_lock
        assert not hooks and not result.spawned
        assert not [e for e in kb.list_events(conn, tid) if e.kind == 'spawned']
