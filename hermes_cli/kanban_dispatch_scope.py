"""Board authorization for native dispatch, including intake and reconciliation."""
from collections.abc import Mapping


def dispatch_board_allowed(board=None):
    """Absent key preserves legacy behavior; present empty/malformed value denies all.

    Read the effective user layer (not defaults) on each tick so revocation does
    not require a restart. This gate precedes any DB mutation or plugin tick.
    """
    from hermes_cli.config_effective import load_user_config_effective
    from hermes_cli import kanban_db as kb
    try:
        cfg = load_user_config_effective(fail_closed=True) or {}
        kanban = cfg.get("kanban", {})
        if not isinstance(kanban, Mapping):
            return False
        if "dispatch_boards" not in kanban:
            return True
        names = kanban["dispatch_boards"]
        if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
            return False
        allowed = {kb._normalize_board_slug(n) for n in names}
        if None in allowed:
            return False
        slug = kb._normalize_board_slug(board) or kb.get_current_board()
        return slug in allowed
    except Exception:
        return False


def dispatch_task_route_allowed(task):
    """Optional strict lane: task overrides cannot redirect the configured route."""
    from hermes_cli.config_effective import load_user_config_effective
    try:
        cfg=load_user_config_effective(fail_closed=True) or {}
        policy=cfg.get("kanban",{}).get("dispatch_no_task_overrides",False)
        if policy is False:return True
        if policy is not True:return False
        return not any(getattr(task,k,None) for k in ("model_override","provider_override","reasoning_effort"))
    except Exception:
        return False
