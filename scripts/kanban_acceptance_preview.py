#!/usr/bin/env python3
"""Local, read-only preview of the app-pinned completion-check fix (card t_856a2933).

Never a gate: nothing here claims, blocks, completes or writes a card. The live board is
only read through SQLite's backup API into a private copy. The page shows two labelled
diagnostics, computed once at startup:

* Replay: GitHub's responses for one PR, captured by ``--capture`` with the AMBIENT ``gh``
  login (a separate read-only diagnostic, never any card's gate), replayed through the
  base collector (32a79afb, from git) and the fixed one. A response missing from the
  capture is an honest evidence gap, never success.
* Cards: the copied board's named cards (id, assignee, PR, state, newest recorded
  receipt) plus the fixed collector's real gate run as that card's assignee, read-only.

Serves GET / on 127.0.0.1 only; every other path is 404 and every other method 405.

    python scripts/kanban_acceptance_preview.py --capture --capture-file CAPTURE.json
    python scripts/kanban_acceptance_preview.py --capture-file CAPTURE.json --port 4123
"""
from __future__ import annotations

import argparse
import html
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import types
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hermes_cli import kanban_pr_acceptance as fixed  # noqa: E402

BASE_REV = "32a79afb"
DEFAULT_PR = "https://github.com/Hjharris1/compound-command/pull/1429"
DEFAULT_CARDS = ("t_e7bbc5cd", "t_e23688d6")
HOST = "127.0.0.1"


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


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
                "at": datetime.fromtimestamp(event[1], timezone.utc).isoformat()}
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


def base_collector() -> types.ModuleType:
    """The collector exactly as installed at BASE_REV, loaded from git (not emulated)."""
    source = subprocess.run(["git", "show", f"{BASE_REV}:hermes_cli/kanban_pr_acceptance.py"], cwd=ROOT,
                            check=True, capture_output=True, text=True, timeout=30).stdout
    module = types.ModuleType("kanban_pr_acceptance_base")
    exec(compile(source, f"{BASE_REV}:hermes_cli/kanban_pr_acceptance.py", "exec"), module.__dict__)
    return module


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


def build_report(board: Path, saved: dict | None, cards, workdir: Path) -> dict:
    """Copy the board, run each card's real gate as its assignee, then replay the capture through both collectors."""
    copy = copy_board(board, workdir)
    report = {"generated_at": _now(), "cards": read_cards(copy, cards), "replay": None}
    for card in report["cards"]:
        if "error" not in card:
            card["gate_now"] = _summary(fixed.collect_acceptance(card["contract"], None, assignee=card["assignee"]))
    if saved:
        report["replay"] = {"captured_at": saved.get("captured_at"), "role": saved.get("role"), "contract": saved["contract"],
                            "before": _summary(replay(base_collector(), saved)), "after": _summary(replay(fixed, saved))}
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
        return '<p class="note">No capture loaded: there is no before/after evidence, which is not a pass.</p>'
    pr = fixed._PR.fullmatch(r["contract"])
    rows = [{"Collector": f"base {BASE_REV} (installed)", "Classification": r["before"]["classification"],
             "Required checks": _checks(r["before"])},
            {"Collector": "fixed (this branch)", "Classification": r["after"]["classification"], "Required checks": _checks(r["after"])}]
    return (f'<p class="note"><strong>Replay fixture, not a live gate.</strong> GitHub responses for PR '
            f'{html.escape("#" + pr[2] if pr else r["contract"])} were captured at {html.escape(str(r["captured_at"]))} '
            f'by the {html.escape(str(r["role"]))}. Both collectors read the same saved responses; '
            "a response missing from the capture shows as infra.</p>"
            + _table(f"Same saved responses, {_head(r['after']['head_sha'])}", rows))


def render(report: dict) -> str:
    at = html.escape(report["generated_at"])
    cards = [_card_row(c) for c in report["cards"]]
    limits = [f"Computed once at {at}; reloading re-reads nothing. Restart the process to refresh.",
              "The replay is saved evidence read by the ambient gh login, a separate read-only diagnostic. "
              "It is never a card&#39;s gate and completes nothing.",
              "Gate now is the fixed collector run once, read-only, as each card&#39;s assignee profile. "
              "Nothing was recorded, claimed, retried or completed.",
              f"The board is a private SQLite backup copy taken at {at} and is not served; the live board may have changed since.",
              "Approving this approves installing the source fix only. Auth config and card-owner fixes are separate decisions."]
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>Completion-check preview</title><style>{CSS}</style></head><body><main>"
            "<h1>Completion-check fix: local read-only preview</h1>"
            f"<h2>PR evidence: before and after</h2>{_replay_section(report['replay'])}"
            "<h2>Cards on the copied board</h2>"
            + (_table(f"Gate run at {report['generated_at']}", cards) if cards else "<p>No cards requested.</p>")
            + "<h2>Limits</h2><ul>" + "".join(f"<li>{item}</li>" for item in limits) + "</ul></main></body></html>")


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


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--board", type=Path, default=Path.home() / ".hermes/kanban/boards/ops/kanban.db")
    parser.add_argument("--capture-file", type=Path, required=True)
    parser.add_argument("--capture", action="store_true", help="refresh the capture with the ambient gh login, then exit")
    parser.add_argument("--pr", default=DEFAULT_PR)
    parser.add_argument("--cards", default=",".join(DEFAULT_CARDS))
    parser.add_argument("--port", type=int, default=4123)
    args = parser.parse_args(argv)
    if args.capture:
        args.capture_file.write_text(json.dumps(capture(args.pr)), encoding="utf-8")
        os.chmod(args.capture_file, 0o600)
        print(f"captured {args.pr} to {args.capture_file}")
        return 0
    try:
        report = build_report(args.board, load_capture(args.capture_file), [c for c in args.cards.split(",") if c],
                              Path(tempfile.mkdtemp(prefix="acceptance-preview-")))
        server = make_server(render(report), args.port)
    except (OSError, ValueError, sqlite3.Error, subprocess.SubprocessError) as exc:
        print(f"preview not started: {exc}", file=sys.stderr)
        return 2
    print(f"preview http://{HOST}:{server.server_address[1]}/ pid {os.getpid()}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
