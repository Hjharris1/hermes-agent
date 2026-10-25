"""App-pinned required checks satisfied by a legacy commit status (PR 1429 shape).

Host-independent: GitHub is faked at the ``_api`` seam with payloads mirroring the captured
PR 1429 evidence (Vercel app 8329 posts a legacy "Vercel" status as vercel[bot] 35613825;
full-suite is a check-run from app 15368). No network, no real profile.
"""
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_pr_acceptance as acc
from hermes_cli.kanban_db_connect import connect

PR = "https://github.com/acme/repo/pull/1429"
HEAD = "5" * 40
VERCEL_BOT = {"login": "vercel[bot]", "id": 35613825, "type": "Bot"}
VERCEL_URL = "https://github.com/apps/vercel"


class FakeGitHub:
    def __init__(self):
        self.head = HEAD
        self.base = "main"
        self.final_head = None
        self.final_base = None
        self.required = [{"context": "Vercel", "app": {"databaseId": 8329}},
                         {"context": "full-suite", "app": {"databaseId": 15368}}]
        self.runs = [{"id": 1, "name": "Vercel Preview Comments", "head_sha": HEAD, "app": {"id": 8329},
                      "status": "completed", "conclusion": "success", "html_url": "https://vercel.com/github"},
                     {"id": 2, "name": "full-suite", "head_sha": HEAD, "app": {"id": 15368},
                      "status": "completed", "conclusion": "success", "html_url": "https://github.com/acme/repo/actions/runs/2"}]
        self.total = None
        self.statuses = [{"id": 57211044491, "context": "Vercel", "state": "pending", "creator": VERCEL_BOT,
                          "target_url": "https://vercel.com/x"},
                         {"id": 57211115837, "context": "Vercel", "state": "success", "creator": VERCEL_BOT,
                          "target_url": "https://vercel.com/x"}]
        self.contexts = [{"context": "Vercel", "state": "SUCCESS", "isRequired": True,
                          "creator": {"__typename": "Bot", "databaseId": 35613825, "url": VERCEL_URL}}]
        self.apps = {"vercel": {"id": 8329, "slug": "vercel", "html_url": VERCEL_URL}}
        self.calls = []
        self.on_app = None

    def __call__(self, endpoint, *, query=None, paginate=False, profile_home=None):
        self.calls.append((endpoint, profile_home))
        if endpoint == "graphql" and "pullRequest(number" in query:
            return {"data": {"repository": {"pullRequest": {
                "headRefOid": self.head, "baseRefName": self.base, "state": "OPEN",
                "baseRef": {"branchProtectionRule": {"requiredStatusChecks": self.required}}}}}}
        if endpoint == "graphql" and "object(oid" in query:
            assert f'"{self.head}"' in query and "isRequired(pullRequestNumber:1429)" in query
            return {"data": {"repository": {"object": {"status": {"contexts": self.contexts}}}}}
        if "/rules/branches/" in endpoint:
            return [[]]
        if "/check-runs" in endpoint:
            return [{"total_count": len(self.runs) if self.total is None else self.total, "check_runs": self.runs}]
        if "/statuses" in endpoint:
            return [self.statuses]
        if endpoint == "repos/acme/repo/pulls/1429":
            return {"head": {"sha": self.final_head or self.head}, "base": {"ref": self.final_base or self.base}, "state": "open"}
        if endpoint.startswith("apps/"):
            if self.on_app:
                self.on_app()
            return self.apps[endpoint[len("apps/"):]]
        raise AssertionError(f"unexpected endpoint {endpoint}")

    def endpoints(self):
        return [e for e, _ in self.calls]


@pytest.fixture
def gh(monkeypatch):
    fake = FakeGitHub()
    monkeypatch.setattr(acc, "_api", fake)
    return fake


def vercel(receipt):
    return [c for c in receipt["checks"] if c["name"] == "Vercel"]


def test_pinned_legacy_status_with_id_bound_app_provenance_is_success(gh):
    receipt = acc.collect_acceptance(PR, None)
    assert (receipt["ok"], receipt["classification"]) == (True, "success")
    [check] = vercel(receipt)
    assert check == {"name": "Vercel", "id": 57211115837, "url": "https://vercel.com/x", "head_sha": HEAD,
                     "classification": "success", "conclusion": "success", "via": "legacy-status",
                     "creator": "vercel[bot]", "creator_id": 35613825, "app_slug": "vercel", "app_id": 8329}
    assert gh.endpoints().count("apps/vercel") == 1


@pytest.mark.parametrize("forge", [
    lambda g: g.statuses[-1].update(creator={"login": "vercel[bot]", "id": 35613825, "type": "User"}),
    lambda g: (g.statuses[-1].update(creator={"login": "other[bot]", "id": 999, "type": "Bot"}),
               g.contexts[0]["creator"].update(databaseId=999, url="https://github.com/apps/other"),
               g.apps.update(other={"id": 4242, "html_url": "https://github.com/apps/other"})),
    lambda g: g.apps["vercel"].update(id=9999),
    lambda g: g.contexts[0].update(isRequired=False),
    lambda g: g.contexts[0]["creator"].update(url="https://github.com/apps/vercel/../evil"),
    lambda g: g.contexts[0]["creator"].update(url="https://evil.example/apps/vercel"),
], ids=["user-with-bot-login", "other-bot", "wrong-app-id", "not-required-on-pr", "non-canonical-path", "other-host"])
def test_forged_or_unproven_status_never_satisfies_the_pin(gh, forge):
    forge(gh)
    receipt = acc.collect_acceptance(PR, None)
    assert (receipt["ok"], receipt["classification"]) == (False, "missing")
    assert vercel(receipt) == [{"name": "Vercel", "classification": "missing", "head_sha": HEAD}]
    assert not any(e.startswith("apps/") and e not in ("apps/vercel", "apps/other") for e in gh.endpoints())


@pytest.mark.parametrize("state, expected", [("pending", "pending"), ("failure", "failure"), ("error", "infra")])
def test_newest_bound_status_decides_even_when_an_older_one_succeeded(gh, state, expected):
    gh.statuses.append({"id": 57211200000, "context": "Vercel", "state": state, "creator": VERCEL_BOT, "target_url": "u"})
    gh.contexts[0]["state"] = state.upper()
    receipt = acc.collect_acceptance(PR, None)
    assert (receipt["ok"], receipt["classification"]) == (False, expected)
    assert [(c["id"], c["classification"]) for c in vercel(receipt)] == [(57211200000, expected)]


@pytest.mark.parametrize("change", ["head", "base"])
def test_head_or_base_change_during_collection_is_stale(gh, change):
    setattr(gh, f"final_{change}", "6" * 40 if change == "head" else "release")
    receipt = acc.collect_acceptance(PR, None)
    assert (receipt["ok"], receipt["classification"]) == (False, "stale")


@pytest.mark.parametrize("break_evidence", [
    lambda g: g.contexts.clear(),
    lambda g: g.contexts.append(dict(g.contexts[0])),
    lambda g: g.contexts[0].update(state="PENDING"),
    lambda g: g.contexts[0]["creator"].update(databaseId=1),
    lambda g: setattr(g, "contexts", None),
    lambda g: setattr(g, "total", 3),
], ids=["context-absent", "context-ambiguous", "state-disagrees", "creator-disagrees", "no-contexts", "short-pagination"])
def test_incomplete_or_disagreeing_evidence_fails_closed_as_infra(gh, break_evidence):
    break_evidence(gh)
    receipt = acc.collect_acceptance(PR, None)
    assert (receipt["ok"], receipt["classification"]) == (False, "infra")


def test_status_and_provenance_reads_are_bound_to_the_exact_head(gh):
    receipt = acc.collect_acceptance(PR, None)
    assert receipt["classification"] == "success"
    assert f"repos/acme/repo/commits/{HEAD}/statuses?per_page=100" in gh.endpoints()
    # FakeGitHub also asserts the status-context query names the exact head oid and PR number.
    assert sum(1 for e in gh.endpoints() if e == "graphql") == 2


def test_unpinned_context_keeps_accepting_its_legacy_status_without_provenance_reads(gh):
    gh.required = [{"context": "ci", "app": None}]
    gh.statuses = [{"id": 5, "context": "ci", "state": "success", "creator": {"login": "someone", "id": 1, "type": "User"}, "target_url": "u"}]
    receipt = acc.collect_acceptance(PR, None)
    assert receipt["classification"] == "success"
    assert not any("object(oid" in str(c) for c in gh.calls) and not any(e.startswith("apps/") for e in gh.endpoints())


def test_no_required_checks_keeps_the_local_only_detail(gh):
    gh.required = []
    receipt = acc.collect_acceptance(PR, None)
    assert (receipt["ok"], receipt["classification"]) == (False, "missing")
    assert "local-only" in receipt["detail"]


def test_every_read_including_provenance_runs_as_the_assignee_profile(gh, monkeypatch):
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "resolve_profile_env", lambda name: f"/profiles/{name}")
    receipt = acc.collect_acceptance(PR, None, assignee="codex")
    assert receipt["classification"] == "success"
    assert {home for _, home in gh.calls} == {"/profiles/codex"}
    assert "apps/vercel" in gh.endpoints()


def test_unresolvable_assignee_is_auth_before_any_github_read(gh, monkeypatch):
    from hermes_cli import profiles

    def missing(name):
        raise FileNotFoundError(name)
    monkeypatch.setattr(profiles, "resolve_profile_env", missing)
    receipt = acc.collect_acceptance(PR, None, assignee="heath")
    assert (receipt["ok"], receipt["classification"]) == (False, "auth")
    assert gh.calls == []


def test_profile_without_login_is_auth_on_the_provenance_read_too(gh, monkeypatch):
    real = gh.__call__

    def no_login(endpoint, **kw):
        if endpoint.startswith("apps/"):
            raise acc._GateAuthError("gh has no login for apps/vercel")
        return real(endpoint, **kw)
    monkeypatch.setattr(acc, "_api", no_login)
    receipt = acc.collect_acceptance(PR, None)
    assert (receipt["ok"], receipt["classification"]) == (False, "auth")


# --- Lifecycle on a real temp SQLite board: closure and the run-ownership race ---

@pytest.fixture
def board(tmp_path, monkeypatch, gh):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    kb.init_db()
    with connect() as conn:
        yield conn


def receipts(conn, tid):
    return [json.loads(r[0]) for r in conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,))]


def test_pinned_vercel_card_closes_with_its_bound_receipt(board, gh):
    tid = kb.create_task(board, title="Publish", completion_contract="acme/repo")
    assert kb.complete_task(board, tid, result="done", metadata={"published_pr": PR})
    assert kb.get_task(board, tid).status == "done"
    [receipt] = receipts(board, tid)
    assert receipt["ok"] and receipt["head_sha"] == HEAD
    assert [(c["id"], c["via"], c["app_id"]) for c in receipt["checks"] if c["name"] == "Vercel"] == [(57211115837, "legacy-status", 8329)]


def test_owner_replaced_while_provenance_is_read_cannot_close_or_record(board, gh):
    tid = kb.create_task(board, title="race", completion_contract="acme/repo")
    run_id = kb.claim_task(board, tid).current_run_id
    replaced = {}

    def reclaim():
        with connect() as rival:
            assert kb.block_task(rival, tid, reason="Reassigned during acceptance")
            assert kb.unblock_task(rival, tid)
            replaced["run"] = kb.claim_task(rival, tid).current_run_id
    gh.on_app = reclaim
    assert not kb.complete_task(board, tid, result="done", expected_run_id=run_id, metadata={"published_pr": PR})
    task = kb.get_task(board, tid)
    assert task.status != "done" and task.current_run_id == replaced["run"] != run_id
    assert receipts(board, tid) == []
