#!/usr/bin/env python3
"""Local, read-only preview of the app-pinned completion-check fix.

Never a gate: nothing here claims, blocks, completes or writes a card, and every input is explicit.

* ``capture --pr URL --capture-file F`` records the GitHub responses the fixed collector reads
  for one PR, using the AMBIENT ``gh`` login: a separate read-only diagnostic, never any card's gate.
* ``serve --base REV --board DB --cards ID [ID ...] --capture-file F`` copies the board through
  SQLite's backup API (read-only source, private copy), runs each named card's real gate once as
  its assignee, and replays the capture through the collector committed at the selected base
  (resolved to one commit) and through this checkout's collector. A response missing from the
  capture is infra, never success. One escaped page is served on 127.0.0.1: GET / only, every
  other path 404 and every other method 405.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import types
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hermes_cli import kanban_pr_acceptance as fixed

COLLECTOR = "hermes_cli/kanban_pr_acceptance.py"
HOST = "127.0.0.1"
SCOPE_NOTE = "Evidence for the collector source change only; profile auth and card ownership are separate decisions."
NO_PASS_NOTE = "This page shows no passing replay of the fixed collector, so it is not evidence for the change."


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def copy_board(source: Path, dest_dir: Path) -> Path:
    """A private copy of the board via SQLite's backup API; the source is opened read-only."""
    dest_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    dest = dest_dir / "board-copy.db"
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    os.chmod(dest, 0o600)
    return dest


def _recorded(event) -> dict | None:
    """The newest receipt's classification, head and clock; an unreadable one says so, never guessed."""
    if event is None:
        return None
    try:
        receipt = json.loads(event[0])
        return {"classification": str(receipt["classification"]), "head_sha": receipt.get("head_sha"),
                "at": datetime.fromtimestamp(event[1], UTC).isoformat()}
    except (ValueError, KeyError, TypeError, OverflowError, OSError):
        return {"classification": "unreadable receipt", "head_sha": None, "at": None}


def read_cards(db: Path, ids) -> list[dict]:
    """Each card's id, assignee, contract, state and newest receipt; gaps are reported, never filled."""
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        cards = []
        for tid in ids:
            row = conn.execute("SELECT id, assignee, completion_contract, status FROM tasks WHERE id=?", (tid,)).fetchone()
            if row is None:
                cards.append({"id": tid, "error": "not on the copied board"})
                continue
            card = dict(zip(("id", "assignee", "contract", "status"), row))
            card["recorded"] = _recorded(conn.execute("SELECT payload, created_at FROM task_events WHERE task_id=? "
                                                      "AND kind='pr_acceptance' ORDER BY id DESC LIMIT 1", (tid,)).fetchone())
            cards.append(card)
        return cards
    finally:
        conn.close()


def resolve_commit(rev: str) -> str:
    """The full hash of the one commit ``rev`` names in this checkout; anything else is refused."""
    if not rev or rev.startswith("-"):
        raise ValueError(f"{rev!r} is not a revision")
    result = subprocess.run(["git", "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"], cwd=ROOT,
                            check=False, capture_output=True, text=True, timeout=30)
    sha = result.stdout.strip()
    if result.returncode or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha):
        raise ValueError(f"{rev!r} does not name a commit in {ROOT}")
    return sha


def base_collector(rev: str) -> tuple[str, types.ModuleType]:
    """The selected base's full hash and its collector, loaded from exactly that commit (never a path)."""
    commit = resolve_commit(rev)
    source = subprocess.run(["git", "show", f"{commit}:{COLLECTOR}"], cwd=ROOT,
                            check=True, capture_output=True, text=True, timeout=30).stdout
    module = types.ModuleType("kanban_pr_acceptance_base")
    exec(compile(source, f"{commit}:{COLLECTOR}", "exec"), module.__dict__)
    return commit, module


def fixed_revision() -> str:
    """HEAD's full hash for the collector imported here, flagged when that file differs from HEAD."""
    if Path(fixed.__file__).resolve() != (ROOT / COLLECTOR).resolve():
        raise ValueError(f"the imported collector {fixed.__file__} is not this checkout's")
    head = resolve_commit("HEAD")
    diff = subprocess.run(["git", "diff", "--quiet", head, "--", COLLECTOR], cwd=ROOT, check=False, capture_output=True, timeout=30)
    if diff.returncode not in (0, 1):
        raise ValueError("cannot compare the imported collector with HEAD")
    return head + (" plus uncommitted collector edits" if diff.returncode else "")


def _key(endpoint: str, query: str | None) -> str:
    return json.dumps([endpoint, query])


def capture(contract: str) -> dict:
    """Record every response the fixed collector reads, using the AMBIENT gh login (diagnostic only)."""
    responses, real = {}, fixed._api

    def recording(endpoint, *, query=None, paginate=False, profile_home=None):
        value = real(endpoint, query=query, paginate=paginate, profile_home=profile_home)
        responses[_key(endpoint, query)] = value
        return value
    fixed._api = recording
    try:
        receipt = fixed.collect_acceptance(contract, None)
    finally:
        fixed._api = real
    return {"captured_at": _now(), "role": "ambient gh read-only diagnostic; not any card's gate",
            "contract": contract, "ambient_classification": receipt["classification"], "responses": responses}


def replay(module: types.ModuleType, saved: dict) -> dict:
    """Run a collector over the saved responses; an uncaptured read is an evidence gap (infra), not success."""
    real = module._api

    def from_capture(endpoint, *, query=None, paginate=False, profile_home=None):
        return saved["responses"][_key(endpoint, query)]
    module._api = from_capture
    try:
        return module.collect_acceptance(saved["contract"], None)
    finally:
        module._api = real


def load_capture(path: Path) -> dict | None:
    """The saved capture, or None when there is none; a malformed file is an error, never an empty green."""
    if not path.exists():
        return None
    saved = json.loads(path.read_text(encoding="utf-8-sig"))
    if not (isinstance(saved, dict) and isinstance(saved.get("contract"), str) and isinstance(saved.get("responses"), dict)):
        raise ValueError(f"{path.name} is not a capture written by --capture")
    return saved


def _summary(receipt: dict) -> dict:
    return {"classification": receipt.get("classification"), "head_sha": receipt.get("head_sha"),
            "checks": [{k: c.get(k) for k in ("name", "classification", "via", "app_id")} for c in receipt.get("checks", [])]}


def build_report(board: Path, saved: dict | None, cards, workdir: Path, base_rev: str) -> dict:
    """Resolve the base, copy the board, run each card's real gate as its assignee, then replay the capture."""
    base, before = base_collector(base_rev)
    copy = copy_board(board, workdir)
    report = {"generated_at": _now(), "cards": read_cards(copy, cards), "replay": None}
    for card in report["cards"]:
        if "error" not in card:
            card["gate_now"] = _summary(fixed.collect_acceptance(card["contract"], None, assignee=card["assignee"]))
    if saved:
        report["replay"] = {"captured_at": saved.get("captured_at"), "role": saved.get("role"), "contract": saved["contract"],
                            "base": base, "fixed": fixed_revision(),
                            "before": _summary(replay(before, saved)), "after": _summary(replay(fixed, saved))}
    return report


CSS = """body{font:16px/1.5 system-ui,sans-serif;margin:0 auto;max-width:72rem;padding:1rem;color:#111;background:#fff}
h1{font-size:1.4rem}h2{font-size:1.15rem;margin-top:2rem}.note{border-left:4px solid #8a5a00;background:#fff8e6;padding:.5rem .75rem}
table{border-collapse:collapse;width:100%}th,td{border:1px solid #888;padding:.4rem .5rem;text-align:left;vertical-align:top}
caption{text-align:left;font-weight:600;padding:.25rem 0;overflow-wrap:anywhere}td{overflow-wrap:anywhere}@media (max-width:640px){table,caption,thead,tbody,tr,th,td{display:block}thead{position:absolute;left:-9999px}
td{border:0;border-bottom:1px solid #ccc}td::before{content:attr(data-label) ": ";font-weight:600}}"""


def _cell(label: str, value) -> str:
    return f'<td data-label="{html.escape(label)}">{html.escape("" if value is None else str(value))}</td>'


def _row(cells: dict) -> str:
    return "<tr>" + "".join(_cell(label, value) for label, value in cells.items()) + "</tr>"


def _table(caption: str, rows: list[dict]) -> str:
    head = "".join(f'<th scope="col">{html.escape(label)}</th>' for label in rows[0])
    return (f"<table><caption>{html.escape(caption)}</caption><thead><tr>{head}</tr></thead><tbody>"
            + "".join(_row(r) for r in rows) + "</tbody></table>")


def _checks(summary: dict) -> str:
    return "; ".join(f"{c['name']}: {c['classification']}" + (f" ({c['via']}, app {c['app_id']})" if c.get("via") else "")
                     for c in summary["checks"]) or "no checks read"


def _head(sha) -> str:
    return f"head {sha}" if sha else "no head read"


def _card_row(card: dict) -> dict:
    if "error" in card:
        return {"Card": card["id"], "Assignee": "", "PR": "", "State": "", "Newest recorded receipt": card["error"],
                "Gate now, as the assignee": "not run"}
    pr = fixed._PR.fullmatch(card["contract"] or "")
    rec, gate = card["recorded"] or {}, card["gate_now"]
    return {"Card": card["id"], "Assignee": card["assignee"], "PR": f"#{pr[2]}" if pr else "not a PR contract",
            "State": card["status"],
            "Newest recorded receipt": f"{rec['classification']} at {rec['at'] or 'an unreadable time'}, {_head(rec['head_sha'])}" if rec else "none",
            "Gate now, as the assignee": f"{gate['classification']}, {_head(gate['head_sha'])}"}


def _replay_section(r: dict | None) -> str:
    if not r:
        return "<h2>PR evidence: none loaded</h2><p class=\"note\">No capture loaded, so there is no before/after evidence.</p>"
    pr = fixed._PR.fullmatch(r["contract"])
    rows = [{"Collector": f"selected comparison base {r['base']}", "Classification": r["before"]["classification"],
             "Required checks": _checks(r["before"])},
            {"Collector": f"fixed revision {r['fixed']}", "Classification": r["after"]["classification"],
             "Required checks": _checks(r["after"])}]
    return (f'<h2>PR evidence: before and after</h2><p class="note"><strong>Replay fixture, not a live gate.</strong> '
            f'GitHub responses for PR {html.escape("#" + pr[2] if pr else r["contract"])} were captured at '
            f'{html.escape(str(r["captured_at"]))} by the {html.escape(str(r["role"]))}. Both collectors read the same '
            "saved responses; a response missing from the capture shows as infra.</p>"
            + _table(f"Same saved responses, {_head(r['after']['head_sha'])}", rows))


def _limits(report: dict, at: str) -> list[str]:
    """Explicit limits; replay and approval wording appear only for the evidence actually loaded."""
    r = report["replay"]
    limits = [f"Computed once at {at}; reloading re-reads nothing. Restart the process to refresh.",
              "Gate now is the fixed collector run once, read-only, as each card&#39;s assignee profile. "
              "Nothing was recorded, claimed, retried or completed.",
              f"The board is a private SQLite backup copy taken at {at} and is not served; the live board may have changed since."]
    if r:
        limits += ["The replay is saved evidence read by the ambient gh login, a separate read-only diagnostic. "
                   "It is never a card&#39;s gate and completes nothing.",
                   "The comparison base is the commit selected at startup; this page does not verify which revision is installed."]
    return limits + [SCOPE_NOTE if r and r["after"]["classification"] == "success" else NO_PASS_NOTE]


def render(report: dict) -> str:
    at = html.escape(report["generated_at"])
    cards = [_card_row(c) for c in report["cards"]]
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>Completion-check preview</title><style>{CSS}</style></head><body><main>"
            "<h1>Completion-check fix: local read-only preview</h1>"
            + _replay_section(report["replay"])
            + "<h2>Cards on the copied board</h2>"
            + (_table(f"Gate run at {report['generated_at']}", cards) if cards else "<p>No cards requested.</p>")
            + "<h2>Limits</h2><ul>" + "".join(f"<li>{item}</li>" for item in _limits(report, at)) + "</ul></main></body></html>")


def make_server(page: str, port: int) -> ThreadingHTTPServer:
    body = page.encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/":
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _refuse(self):
            self.send_error(405)
        do_POST = do_PUT = do_PATCH = do_DELETE = _refuse

        def log_message(self, *args):
            pass
    return ThreadingHTTPServer((HOST, port), Handler)


def _pr_url(value: str) -> str:
    if not fixed._PR.fullmatch(value):
        raise argparse.ArgumentTypeError("needs an exact https://github.com/OWNER/REPO/pull/N URL")
    return value


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    record = commands.add_parser("capture", help="record one PR's GitHub reads with the ambient gh login, then exit")
    record.add_argument("--pr", type=_pr_url, required=True)
    record.add_argument("--capture-file", type=Path, required=True)
    serve = commands.add_parser("serve", help="serve the read-only page on 127.0.0.1")
    serve.add_argument("--base", required=True, help="comparison base revision, resolved to one commit")
    serve.add_argument("--board", type=Path, required=True, help="kanban.db to copy; opened read-only")
    serve.add_argument("--cards", nargs="+", required=True, metavar="TASK_ID")
    serve.add_argument("--capture-file", type=Path, required=True)
    serve.add_argument("--port", type=int, default=4123)
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.command == "capture":
        args.capture_file.write_text(json.dumps(capture(args.pr)), encoding="utf-8")
        os.chmod(args.capture_file, 0o600)
        print(f"captured {args.pr} to {args.capture_file}")
        return 0
    try:
        report = build_report(args.board, load_capture(args.capture_file), args.cards,
                              Path(tempfile.mkdtemp(prefix="acceptance-preview-")), args.base)
        server = make_server(render(report), args.port)
    except (OSError, ValueError, sqlite3.Error, subprocess.SubprocessError) as exc:
        print(f"preview not started: {exc}", file=sys.stderr)
        return 2
    print(f"preview http://{HOST}:{server.server_address[1]}/ pid {os.getpid()}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
