---
title: Mandatory Kanban dispatch guards
---

External backlog adapters must verify their own current source authority before
native Kanban workers begin work. Observer hooks are notifications and cannot
veto dispatch. A plugin can register an explicitly required authorization guard:

```python
def register(ctx):
    ctx.register_kanban_dispatch_guard("contract", verify_current_contract)
```

```yaml
plugins:
  enabled: [your-backlog-plugin]
kanban:
  dispatch_guards: ["your-backlog-plugin:contract"]
```

The callback receives `task`, `phase`, `board`, `profile_home`, and `tool_call_id`
keyword arguments. `task` is a detached, immutable mapping; nested lists become
tuples. `phase` is `claim` or `spawn`. Return **literal `True`** only after checking
current application authority and exact execution identity. Missing, disabled,
unloaded, malformed, throwing or timed-out required guards refuse execution.
Registration names are owned by the plugin and disposed on unload. Guards share
a five-second total callback budget per decision using the native bounded
callback machinery. They must be side-effect-free: a timed-out callback may
finish later, and installed Python plugins are trusted code, not a sandbox.

No guards are required by default, preserving legacy installations. Configuration
must be explicitly enabled in the dispatch home; an application adapter does
not grant itself board/profile access. Existing board, profile and task-route
restrictions still apply. Empty/malformed board allowlists never authorize work.

The native dispatcher checks required guards before claiming either execution or
review work, then checks fresh task/configuration state after workspace setup.
A refused review gives back its unused reserved capacity to eligible ready work.
The dry-run path performs no authorization-related writes. Real pre-claim denials
are exposed in `DispatchResult.authorization_denied`, in suppression summaries,
and in a sanitized `dispatch_authorization_denied` event at most once per minute
per repeated reason. No callback exception text is included in these notices.

After claim, refusal uses the native `capability` block transition with both run
and claim ownership checks, releasing only that run. It does **not** consume the
worker failure budget or silently auto-resume. Resume requires resolving the
cause and using the native lifecycle with current evidence.

For guarded native CLI workers, the parent persists a `dispatch_authorized`
binding in the task's existing event log. It binds dispatch home, board, task
contract digest and effective policy digest to the current run. The child uses
the existing task/run/claim/database environment pins, reads the database without
initialization or migration, and independently rechecks ownership, current policy
and guards in the **dispatch home**, before a single-query turn starts. A mismatch
closes only the owned run as an authorization wait and exits 78 before model
credentials or turn execution. The event contains digests, never configuration
values or claim tokens.

This is cooperative launch authorization for the native CLI worker, not a new
grant of product authority, an OS security boundary, or continuous revocation of
an active turn. The database and configured Python plugins are trusted control
plane code. A custom `spawn_fn`/external worker adapter must provide equivalent
child validation before its first turn. External state may change after the last
check; tool-level authorization remains necessary during work. No service is
restarted, new dispatcher launched, board imported, or canonical backlog updated
by this extension.
