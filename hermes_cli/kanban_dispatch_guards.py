"""Mandatory plugin authorization at native claim and spawn boundaries.

Observer hooks cannot veto execution. These named, explicitly configured guards
must each return literal True; missing, malformed, unavailable or slow guards deny.
There is no vendor policy or admission authority in this module.
"""
from collections.abc import Mapping
from dataclasses import asdict
import logging
import re
import time
from types import MappingProxyType

_GUARD_TIMEOUT_SECONDS = 5.0  # total callback budget, never disabled by hook settings
_MAX_GUARDS = 16
_NAME = re.compile(r"[a-zA-Z0-9_.-]+(?:/[a-zA-Z0-9_.-]+)*:[a-zA-Z0-9_.-]+\Z")
_log = logging.getLogger(__name__)


def _freeze(value):
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def dispatch_guards_allowed(task, *, phase, board):
    """Re-read effective policy each time; no DB/config/model mutations here.

    Callbacks receive detached immutable task data, a normalized board and phase.
    A configured plugin is trusted Python code, not a sandbox. Its policy check
    must be side-effect free. Native timeout suppression bounds abandoned workers.
    """
    from hermes_cli.config_effective import load_user_config_effective
    from hermes_cli import kanban_db as kb
    from hermes_constants import get_hermes_home, hermes_home_key
    try:
        config = load_user_config_effective(fail_closed=True) or {}
        kanban = config.get("kanban", {})
        if not isinstance(kanban, Mapping):
            return False
        required = kanban.get("dispatch_guards", [])
        if (not isinstance(required, list) or len(required) > _MAX_GUARDS
                or any(not isinstance(name, str) or not _NAME.fullmatch(name) for name in required)
                or len(set(required)) != len(required)):
            return False
        if not required:
            return True  # preserves existing installations
        if phase not in ("claim", "spawn") or task is None:
            return False
        from hermes_cli.plugins import _delivery_manager
        manager = _delivery_manager()
        if manager.scope_key != hermes_home_key(get_hermes_home()):
            return False
        payload = dict(task=_freeze(asdict(task)), phase=phase,
                       board=kb._normalize_board_slug(board) or kb.get_current_board(),
                       profile_home=str(get_hermes_home()),
                       tool_call_id=f"kanban:{task.id}:{phase}")
        plugin_config = config.get("plugins", {})
        if not isinstance(plugin_config, Mapping):
            return False
        disabled = plugin_config.get("disabled", [])
        enabled = plugin_config.get("enabled")
        if (not isinstance(disabled, list) or any(not isinstance(x, str) for x in disabled)
                or (enabled is not None and (not isinstance(enabled, list)
                    or any(not isinstance(x, str) for x in enabled)))):
            return False
        from hermes_cli.plugins_discovery import gate_manifest
        deadline = time.monotonic() + _GUARD_TIMEOUT_SECONDS
        for name in required:
            owner = name.split(":", 1)[0]
            loaded = manager._plugins.get(owner)
            if (loaded is None or not loaded.enabled
                    or gate_manifest(loaded.manifest, set(disabled),
                        set(enabled) if enabled is not None else None).action not in ("load", "load_now")):
                return False
            callback = manager._kanban_dispatch_guards.get(name)
            remaining = deadline - time.monotonic()
            if callback is None or remaining <= 0:
                return False
            answer = manager._run_hook_callback_bounded(
                "kanban_dispatch_guard", callback, payload, remaining)
            if (answer is not True or manager._kanban_dispatch_guards.get(name) is not callback
                    or manager._plugins.get(owner) is not loaded):
                return False
        latest = load_user_config_effective(fail_closed=True) or {}
        return (latest.get("kanban", {}).get("dispatch_guards", []) == required
                and latest.get("plugins", {}) == plugin_config)
    except (Exception, SystemExit):
        # Do not expose callback/config exception text: it may contain credentials.
        _log.warning("Kanban dispatch authorization unavailable; refusing %s", phase)
        return False
