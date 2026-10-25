"""The local completion-check preview: a read-only board copy, an honest before/after replay,
an escaped page and a GET-only loopback server.

Host-independent: GitHub is faked at the collector's ``_api`` seam with the PR 1429 shape, boards
are temp SQLite under a temp ``HERMES_HOME`` (no profile resolves there), and "before" is the real
base collector loaded from git.
"""
import hashlib
import http.client
import json
import threading

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_pr_acceptance as acc
from hermes_cli.kanban_db_connect import connect
from scripts import kanban_acceptance_preview as preview
from tests.hermes_cli.test_kanban_pr_acceptance_provenance import PR, FakeGitHub

TAMPERED = "<script>alert(1)</script>"


@pytest.fixture
def gh(monkeypatch):
    fake = FakeGitHub()
    monkeypatch.setattr(acc, "_api", fake)
    return fake


@pytest.fixture
def board(tmp_path, monkeypatch, gh):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    kb.init_db()
    with connect() as conn:
        yield conn


def digest(db):
    return [hashlib.sha256(p.read_bytes()).hexdigest() for p in (db, db.with_name(db.name + "-wal")) if p.exists()]


def checks(receipt):
    return [(c["name"], c["classification"], c.get("via")) for c in receipt["checks"]]


def test_replay_of_one_capture_is_missing_on_base_and_success_on_the_fix(gh):
    saved = preview.capture(PR)
    assert {home for _, home in gh.calls} == {None}, "the capture is the ambient login, never a card's profile"
    assert "ambient" in saved["role"] and "not any card's gate" in saved["role"]
    before = preview.replay(preview.base_collector(), saved)
    after = preview.replay(acc, saved)
    assert (before["classification"], checks(before)) == ("missing", [("Vercel", "missing", None), ("full-suite", "success", None)])
    assert (after["classification"], checks(after)) == ("success", [("Vercel", "success", "legacy-status"), ("full-suite", "success", None)])


@pytest.mark.parametrize("gap", ["provenance-read", "everything"])
def test_a_read_missing_from_the_capture_is_infra_never_success(gh, gap):
    saved = preview.capture(PR)
    if gap == "everything":
        saved["responses"].clear()
    else:
        del saved["responses"][json.dumps(["apps/vercel", None])]
    assert preview.replay(acc, saved)["classification"] == "infra"


def test_missing_or_malformed_capture_files_never_render_a_pass(tmp_path):
    assert preview.load_capture(tmp_path / "absent.json") is None
    for body in ("{", "[]", '{"contract": "x"}'):
        (tmp_path / "bad.json").write_text(body, encoding="utf-8")
        with pytest.raises(ValueError):
            preview.load_capture(tmp_path / "bad.json")
    page = preview.render({"generated_at": "t", "cards": [], "replay": None})
    assert "No capture loaded" in page and "success" not in page


def test_board_copy_is_private_and_cards_keep_their_own_auth_gate(board, gh, tmp_path):
    tid = kb.create_task(board, title="Publish", assignee="heath", completion_contract=PR)
    assert not kb.complete_task(board, tid, result="done")  # heath resolves to no profile here: a real auth receipt
    saved = preview.capture(PR)
    gh.calls.clear()
    source = kb.kanban_db_path()
    before = digest(source)
    report = preview.build_report(source, saved, [tid, "t_absent"], tmp_path / "copy")
    assert digest(source) == before
    assert (tmp_path / "copy" / "board-copy.db").stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "copy").stat().st_mode & 0o777 == 0o700
    card, absent = report["cards"]
    assert (card["recorded"]["classification"], card["gate_now"]["classification"]) == ("auth", "auth")
    assert absent == {"id": "t_absent", "error": "not on the copied board"}
    assert report["replay"]["after"]["classification"] == "success"
    assert gh.calls == [], "the replay's pass comes from the capture and never reaches the card's gate"


def test_bad_and_tampered_records_render_escaped_and_labelled_for_small_screens(board, tmp_path):
    bad = kb.create_task(board, title="t", assignee="heath", completion_contract=PR)
    tampered = kb.create_task(board, title="t", assignee="heath", completion_contract=PR)
    board.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, 'pr_acceptance', '{', 0)", (bad,))
    board.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, 'pr_acceptance', ?, 0)",
                  (tampered, json.dumps({"classification": TAMPERED})))
    board.commit()
    saved = {**preview.capture(PR), "role": TAMPERED, "captured_at": TAMPERED}
    report = preview.build_report(kb.kanban_db_path(), saved, [bad, tampered], tmp_path / "copy")
    assert report["cards"][0]["recorded"]["classification"] == "unreadable receipt"
    page = preview.render(report)
    assert TAMPERED not in page and page.count(html_escaped(TAMPERED)) == 3
    assert '<meta name="viewport" content="width=device-width,initial-scale=1">' in page and "@media (max-width:640px)" in page
    assert page.count("<td") == page.count('<td data-label="') > 0
    assert page.count('<th scope="col">') == 9


def html_escaped(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def request(port, method, path, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request(method, path, body=body)
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


def test_server_is_loopback_get_only_and_exposes_nothing_but_the_page(board, tmp_path):
    tid = kb.create_task(board, title="t", assignee="heath", completion_contract=PR)
    source = kb.kanban_db_path()
    page = preview.render(preview.build_report(source, None, [tid], tmp_path / "copy"))
    before = digest(source)
    server = preview.make_server(page, 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        host, port = server.server_address
        assert host == "127.0.0.1"
        assert request(port, "GET", "/") == (200, page.encode())
        for path in ("/board-copy.db", "/kanban.db", "/../kanban.db", f"/{tmp_path}/copy/board-copy.db", "/capture.json", "/?x=1"):
            assert request(port, "GET", path)[0] == 404, path
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            assert request(port, method, "/", body=b'{"complete": true}')[0] == 405, method
        assert request(port, "GET", "/") == (200, page.encode())
    finally:
        server.shutdown()
        server.server_close()
    assert digest(source) == before
    assert kb.get_task(board, tid).status != "done"
