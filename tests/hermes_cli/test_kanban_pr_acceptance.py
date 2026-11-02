"""Two lifecycle invariants, using real SQLite and a local GitHub HTTP contract."""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect
from hermes_cli import kanban_pr_acceptance as accept


@pytest.fixture
def github(tmp_path, monkeypatch):
    state = {"conclusion": "success", "head": "a" * 40, "reads": 0, "requests": []}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state["requests"].append(self.path)
            sha = state["head"]
            if self.path == "/graphql":
                value = {"data": {"repository": {"pullRequest": {
                    "headRefOid": sha, "baseRefName": "main", "state": "OPEN",
                    "baseRef": {"branchProtectionRule": {"requiredStatusChecks": [
                        {"context": "required", "app": {"databaseId": 1}}]}}}}}}
            elif "/rules/branches/" in self.path:
                value = [[]]
            elif "/check-runs" in self.path:
                run = {"id": 42, "name": "required", "head_sha": sha,
                       "app": {"id": 1}, "status": "in_progress" if state["conclusion"] == "pending" else "completed", "conclusion": state["conclusion"],
                       "html_url": "https://github.com/acme/repo/actions/runs/42"}
                if state.get("stale"):
                    run["head_sha"] = "b" * 40
                runs = [] if state.get("missing") else [run]
                value = [{"total_count": 100 + len(runs), "check_runs": [
                    {**run, "id": 1000 + i, "name": "optional", "conclusion": "skipped"}
                    for i in range(100)]}, {"total_count": 100 + len(runs), "check_runs": runs}]
                if state.get("race"):
                    state["race"]()
                if state.get("head_change"):
                    state["head"] = "b" * 40
            elif "/statuses" in self.path:
                value = [[]]
            elif "/pulls/" in self.path:
                value = {"head": {"sha": sha}, "base": {"ref": "main"}, "state": "open"}
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(value).encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    shim = tmp_path / "bin"
    shim.mkdir()
    gh = shim / "gh"
    gh.write_text(f"#!{sys.executable}\nimport sys,urllib.request\n"
                  f"u='http://127.0.0.1:{server.server_port}/'+sys.argv[2]\n"
                  "print(urllib.request.urlopen(u).read().decode())\n")
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", str(shim) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    kb.init_db()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.platforms("linux")
def test_pr_completion_requires_current_required_evidence(github):
    with connect() as conn:
        for conclusion in ("failure", "pending", "cancelled", "timed_out", "action_required", "neutral", "skipped", None, "success"):
            github.update(conclusion=conclusion, head="a" * 40)
            tid = kb.create_task(conn, title="Publish", completion_contract="acme/repo")
            ok = kb.complete_task(conn, tid, result="done", metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
            assert ok is (conclusion == "success")
            task = kb.get_task(conn, tid)
            assert (task.status == "done") is ok
            receipts = [json.loads(r[0]) for r in conn.execute(
                "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,))]
            assert receipts and receipts[-1]["head_sha"] == "a" * 40
            if not ok:
                assert task.status in {"running", "ready", "blocked", "review"}
                assert "retry" in receipts[-1]["recovery"]
                assert receipts[-1]["checks"][0]["id"] == 42
        for fault in ("missing", "stale", "head_change"):
            github.update(conclusion="success", head="a" * 40)
            github[fault] = True
            tid = kb.create_task(conn, title=fault, completion_contract="acme/repo")
            assert not kb.complete_task(conn, tid, result="done", metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
            assert kb.get_task(conn, tid).status != "done"
            github.pop(fault)
        # Omission and a sibling repository cannot downgrade the stored declaration.
        tid = kb.create_task(conn, title="publish", completion_contract="acme/repo")
        assert not kb.complete_task(conn, tid, summary="local green")
        assert not kb.complete_task(conn, tid, result="done", metadata={"published_pr": "https://github.com/other/repo/pull/7"})
        before = len(github["requests"])
        local = kb.create_task(conn, title="local", completion_contract="local-only")
        assert kb.complete_task(conn, local, summary="https://github.com/acme/repo/pull/7 is background context")
        assert len(github["requests"]) == before


@pytest.mark.platforms("linux")
def test_acceptance_receipts_and_terminal_write_share_run_ownership(github):
    with connect() as conn:
        for conclusion in ("success", "failure"):
            tid = kb.create_task(conn, title="race", completion_contract="acme/repo")
            owner = kb.claim_task(conn, tid)
            run_id = owner.current_run_id
            def reclaim():
                with connect() as rival:
                    assert kb.block_task(rival, tid, reason="Reassigned during acceptance")
                    assert kb.unblock_task(rival, tid)
                    github["replacement"] = kb.claim_task(rival, tid).current_run_id
            github.update(conclusion=conclusion, race=reclaim)
            assert not kb.complete_task(conn, tid, result="done", expected_run_id=run_id,
                metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
            assert kb.get_task(conn, tid).current_run_id == github["replacement"]
            assert github["replacement"] != run_id
            assert kb.get_task(conn, tid).status != "done"
            assert conn.execute("SELECT count(*) FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,)).fetchone()[0] == 0
            github.pop("race")


def test_closed_absorption_requires_exact_landed_evidence(monkeypatch):
    repo = "acme/repo"
    source = f"https://github.com/{repo}/pull/7"
    vehicle = f"https://github.com/{repo}/pull/8"
    a, b, c, d = (letter * 40 for letter in "abcd")
    source_pr = {"head": {"sha": a, "ref": "feature", "repo": {"full_name": repo}}, "base": {"ref": "staging"},
                 "state": "closed", "merged": False, "changed_files": 1}
    train_pr = {"head": {"sha": b, "ref": "integra/trem-test", "repo": {"full_name": repo}}, "base": {"ref": "staging"},
                "state": "closed", "merged": True, "merge_commit_sha": c}
    state = {"source": source_pr, "train": train_pr, "files": [[{"filename": "file.py", "sha": d, "status": "modified"}]],
             "source_tree": {"file.py": d}, "train_tree": {"file.py": d},
             "landing_tree": {"file.py": d}, "compare": "ahead", "source_check": "success", "train_check": "success"}

    def api(endpoint, **kwargs):
        if endpoint == "graphql":
            n = 7 if "number:7" in kwargs["query"] else 8
            pr = state["source" if n == 7 else "train"]
            return {"data": {"repository": {"pullRequest": {
                "headRefOid": pr["head"]["sha"], "baseRefName": pr["base"]["ref"],
                "state": "MERGED" if pr["merged"] else "CLOSED",
                "baseRef": {"branchProtectionRule": {"requiredStatusChecks": [
                    {"context": "required", "app": {"databaseId": 1}}]}}}}}}
        if "/rules/branches/" in endpoint:
            return [[]]
        if "/check-runs" in endpoint:
            is_source = a in endpoint
            sha = a if is_source else b
            conclusion = state["source_check" if is_source else "train_check"]
            attempts = conclusion if isinstance(conclusion, list) else [(42, conclusion, 1, sha)]
            runs = [{"id": ident, "name": "required", "head_sha": run_sha,
                "app": {"id": app}, "status": "completed", "conclusion": result}
                for ident, result, app, run_sha in attempts]
            return [{"total_count": len(runs), "check_runs": runs}]
        if "/statuses" in endpoint:
            return [[]]
        if endpoint.endswith("/pulls/7/files?per_page=100"):
            return state["files"]
        if endpoint.endswith("/pulls/7"):
            return state["source"]
        if endpoint.endswith("/pulls/8"):
            return state["train"]
        if "/git/trees/" in endpoint:
            key = "source_tree" if a in endpoint else "train_tree" if b in endpoint else "landing_tree"
            return {"truncated": False, "tree": [{"path": path, "sha": blob[-1] if isinstance(blob, tuple) else blob,
                "mode": blob[0] if isinstance(blob, tuple) else "100644", "type": "blob"}
                for path, blob in state[key].items()]}
        if endpoint.endswith("/branches/staging"):
            return {"commit": {"sha": c}}
        if "/compare/" in endpoint:
            return {"status": state["compare"]}
        raise AssertionError(endpoint)

    monkeypatch.setattr(accept, "_api", api)
    def collect():
        return accept.collect_acceptance("github_pr", source, integration_pr=vehicle)
    assert accept.collect_acceptance("github_pr", source)["ok"] is False
    assert collect()["ok"] is True
    assert collect()["integration"]["head_sha"] == b
    state["source_check"] = [(41, "failure", 1, a), (42, "success", 1, a)]
    assert collect()["ok"] is True
    state["source_check"] = [(41, "success", 1, a), (42, "failure", 1, a)]
    assert collect()["ok"] is False
    state["source_check"] = [(41, "success", 1, a), (42, "success", 2, a)]
    assert collect()["ok"] is True  # another app cannot replace or veto the pinned app
    state["source_check"] = [(42, "success", 2, a)]
    assert collect()["ok"] is False
    state["source_check"] = [(41, "success", 1, a), (42, "success", 1, b)]
    assert collect()["ok"] is False
    state["source_check"] = "success"
    for key, value in [("source_check", "failure"), ("train_check", "pending"),
                       ("compare", "diverged"), ("files", []), ("landing_tree", {}),
                       ("train_tree", {}), ("train_tree", {"file.py": ("100755", d)}),
                       ("source", {**source_pr, "head": {**source_pr["head"], "repo": {"full_name": "other/repo"}}}),
                       ("train", {**train_pr, "head": {**train_pr["head"], "ref": "other"}}),
                       ("train", {**train_pr, "head": {**train_pr["head"], "repo": {"full_name": "other/repo"}}}),
                       ("files", [[{"filename": "file.py", "sha": d, "status": "renamed"}]])]:
        original = state[key]
        state[key] = value
        assert not collect()["ok"], (key, value)
        state[key] = original
    state["files"] = [[{"filename": "file.py", "previous_filename": "old.py", "sha": d, "status": "renamed"}]]
    state["source_tree"] = {"old.py": d, "file.py": d}
    assert not collect()["ok"]
    state["files"] = [[{"filename": "file.py", "sha": d, "status": "modified"}]]
    state["source_tree"] = {"file.py": d}
    state["source"] = {**source_pr, "merged": False, "state": "open"}
    assert not collect()["ok"]
    state["source"] = source_pr
    assert not accept.collect_acceptance("github_pr", source,
        integration_pr="https://github.com/other/repo/pull/8")["ok"]


def test_integration_binding_survives_retry_and_cannot_be_replaced(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    kb.init_db()
    from hermes_cli import kanban_pr_acceptance_store as store
    calls = []
    def collect(contract, published, *, integration_pr=None):
        calls.append((contract, published, integration_pr))
        return {"ok": False, "classification": "pending", "head_sha": "a" * 40,
                "checks": [], "recovery": "retry"}
    monkeypatch.setattr(store, "collect_acceptance", collect)
    source = "https://github.com/acme/repo/pull/7"
    train = "https://github.com/acme/repo/pull/8"
    with connect() as conn:
        tid = kb.create_task(conn, title="delivery", completion_contract="github_pr")
        assert not kb.complete_task(conn, tid, summary="verified PR delivery", metadata={"published_pr": source, "integration_pr": train})
        assert not kb.complete_task(conn, tid, summary="verified PR delivery", metadata={"published_pr": source})
        assert calls == [("github_pr", source, train)] * 2
        assert not kb.complete_task(conn, tid, summary="verified PR delivery", metadata={"published_pr": source,
            "integration_pr": "https://github.com/acme/repo/pull/9"})
        assert len(calls) == 2
        assert conn.execute("SELECT count(*) FROM task_events WHERE task_id=? AND kind='pr_integration'", (tid,)).fetchone()[0] == 1
