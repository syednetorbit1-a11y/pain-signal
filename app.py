"""Pain Signal — local web UI for the Reddit -> Claude market research pipeline.

Run:  python app.py        then open http://127.0.0.1:8820
"""
import html
import json
import os
import re
import shutil
import threading
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path

import anthropic
import markdown
from dotenv import load_dotenv, set_key
from flask import Flask, abort, jsonify, render_template, request, send_file

import scraper

BASE = Path(__file__).resolve().parent
RUNS = BASE / "runs"
ENV_FILE = BASE / ".env"
HOST, PORT = "127.0.0.1", int(os.getenv("PAIN_SIGNAL_PORT", "8820"))

ENV_KEYS = {
    "reddit_client_id": "REDDIT_CLIENT_ID",
    "reddit_client_secret": "REDDIT_CLIENT_SECRET",
    "reddit_user_agent": "REDDIT_USER_AGENT",
    "anthropic_api_key": "ANTHROPIC_API_KEY",
}
TIME_FILTERS = ["day", "week", "month", "year", "all"]
RUN_ID = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{6}$")

RUNS.mkdir(exist_ok=True)
load_dotenv(ENV_FILE)
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024

_lock = threading.Lock()   # guards _active and meta writes
_active = {"id": None, "cancel": None}


class Cancelled(Exception):
    pass


# ------------------------------------------------------------------ storage

def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _run_dir(run_id):
    if not RUN_ID.match(run_id or ""):
        abort(404)
    d = RUNS / run_id
    if not d.is_dir():
        abort(404)
    return d


def _read_meta(d):
    return json.loads((d / "meta.json").read_text(encoding="utf-8"))


def _write_meta(d, meta):
    tmp = d / "meta.json.tmp"
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, d / "meta.json")


def _update(d, **fields):
    with _lock:
        meta = _read_meta(d)
        meta.update(fields)
        _write_meta(d, meta)
        return meta


def _render_report(md_text):
    # Reddit text flows into the report, so raw HTML is never allowed through.
    out = markdown.markdown(md_text.replace("<", "&lt;"), extensions=["tables", "fenced_code", "sane_lists"])

    def safe_link(m):
        url = html.unescape(m.group(1))
        if not url.startswith(("https://", "http://")):
            return "href=\"#\""
        return f"href=\"{html.escape(url)}\" target=\"_blank\" rel=\"noopener noreferrer\""
    return re.sub(r'href="([^"]*)"', safe_link, out)


# ------------------------------------------------------------------ pipeline worker

def _keys_status():
    key = os.getenv("ANTHROPIC_API_KEY", "")
    return {
        "reddit": bool(os.getenv("REDDIT_CLIENT_ID") and os.getenv("REDDIT_CLIENT_SECRET")),
        "anthropic": bool(key),
        "anthropic_hint": f"…{key[-4:]}" if len(key) > 8 else "",
        "reddit_user_agent": os.getenv("REDDIT_USER_AGENT", ""),
    }


def _friendly_error(e):
    if isinstance(e, anthropic.AuthenticationError):
        return "Claude rejected the API key. Update it in Settings."
    if isinstance(e, anthropic.RateLimitError):
        return "Claude rate limit reached. Wait a minute, then choose Re-run analysis."
    if isinstance(e, anthropic.APIStatusError):
        return f"Claude API error {e.status_code}: {e.message}"
    if isinstance(e, anthropic.APIConnectionError):
        return "Couldn't reach Claude. Check the internet connection, then choose Re-run analysis."
    msg = str(e)
    if "401" in msg or "invalid_grant" in msg:
        return "Reddit rejected the credentials. Check the client ID and secret in Settings."
    return msg or e.__class__.__name__


def _worker(d, scrape_first, cancel):
    meta = _read_meta(d)
    p = meta["params"]

    def check():
        if cancel.is_set():
            raise Cancelled()

    def on_progress(ev):
        check()
        with _lock:
            m = _read_meta(d)
            if ev["type"] == "search_start":
                m["grid"][f"{ev['subreddit']}|{ev['keyword']}"] = {"state": "searching"}
            elif ev["type"] == "search_done":
                cell = {"state": "error", "error": ev["error"]} if ev.get("error") else \
                       {"state": "done", "found": ev["found"]}
                m["grid"][f"{ev['subreddit']}|{ev['keyword']}"] = cell
                m["posts"] = ev.get("total", m.get("posts", 0))
            elif ev["type"] == "chunk_start":
                m["analysis"] = {"step": "reading", "index": ev["index"], "total": ev["total"]}
            elif ev["type"] == "report_start":
                m["analysis"] = {"step": "writing", "quotes": ev["quotes"], "dropped": ev["dropped"]}
            _write_meta(d, m)

    try:
        if scrape_first:
            _update(d, status="scraping")
            posts = scraper.scrape(scraper.make_reddit(), p["time"], p["limit"], p["comments"],
                                   p["subreddits"], p["keywords"], on_progress=on_progress)
            check()
            if not posts:
                raise RuntimeError("No matching posts. Widen the time range or add keywords.")
            scraper.save_posts(posts, d / "data.json", p["subreddits"], p["keywords"])
            _update(d, posts=len(posts))

        _update(d, status="analyzing", analysis={"step": "reading", "index": 0, "total": 0}, error=None)
        usage = {}
        try:
            stats = scraper.analyze(str(d / "data.json"), str(d / "report.md"), on_progress=on_progress, usage=usage)
        finally:
            prev = _read_meta(d).get("usage") or {}
            _update(d, usage={k: prev.get(k, 0) + usage.get(k, 0) for k in ("input_tokens", "output_tokens", "cost_usd")})
        _update(d, status="done", finished=_now(), quotes=stats["quotes"], dropped=stats["dropped"], analysis=None)
    except Cancelled:
        _update(d, status="cancelled", finished=_now(), analysis=None)
    except Exception as e:  # surface every failure in the UI instead of a dead thread
        traceback.print_exc()
        _update(d, status="failed", finished=_now(), error=_friendly_error(e), analysis=None)
    finally:
        with _lock:
            _active.update(id=None, cancel=None)


def _start(d, scrape_first):
    with _lock:
        if _active["id"]:
            return False
        cancel = threading.Event()
        _active.update(id=d.name, cancel=cancel)
    threading.Thread(target=_worker, args=(d, scrape_first, cancel), daemon=True).start()
    return True


def _recover_interrupted():
    """Runs left mid-flight by a server restart are marked failed."""
    for d in RUNS.iterdir():
        if (d / "meta.json").exists():
            m = _read_meta(d)
            if m["status"] in ("queued", "scraping", "analyzing"):
                m.update(status="failed", error="The app was stopped while this run was in progress.",
                         analysis=None, finished=_now())
                _write_meta(d, m)


# ------------------------------------------------------------------ routes

@app.before_request
def guard_writes():
    """Block cross-site requests: writes must be same-origin JSON."""
    if request.method in ("POST", "DELETE") and request.path.startswith("/api/"):
        origin = request.headers.get("Origin")
        if origin and origin != request.host_url.rstrip("/"):
            abort(403)
        if not request.is_json:
            abort(415)


@app.errorhandler(400)
@app.errorhandler(403)
@app.errorhandler(404)
@app.errorhandler(409)
@app.errorhandler(415)
def api_error(e):
    return jsonify(error=e.description), e.code


@app.get("/")
def index():
    v = int(max((BASE / "static" / f).stat().st_mtime for f in ("app.css", "app.js")))
    return render_template("index.html", v=v)


@app.get("/api/status")
def status():
    return jsonify(keys=_keys_status(), active=_active["id"],
                   defaults={"subreddits": scraper.SUBREDDITS, "keywords": scraper.KEYWORDS,
                             "time": "year", "limit": 25, "comments": 5, "time_filters": TIME_FILTERS},
                   model=scraper.MODEL)


@app.post("/api/settings")
def save_settings():
    body = request.get_json(silent=True) or {}
    ENV_FILE.touch(exist_ok=True)
    changed = []
    for field, env in ENV_KEYS.items():
        val = str(body.get(field) or "").strip()
        if val:
            if len(val) > 400 or "\n" in val:
                abort(400, f"{field} looks invalid.")
            set_key(str(ENV_FILE), env, val, quote_mode="never")
            os.environ[env] = val
            changed.append(field)
    return jsonify(saved=changed, keys=_keys_status())


def _clean_list(values, pattern, cap):
    out = []
    for v in values or []:
        v = str(v).strip()
        if v and re.fullmatch(pattern, v) and v not in out:
            out.append(v)
    return out[:cap]


@app.post("/api/runs")
def create_run():
    body = request.get_json(silent=True) or {}
    keys = _keys_status()
    if not keys["reddit"]:
        abort(400, "Add your Reddit client ID and secret in Settings first.")
    if not keys["anthropic"]:
        abort(400, "Add your Anthropic API key in Settings first.")

    subs = _clean_list([s.removeprefix("r/") for s in body.get("subreddits", [])], r"[A-Za-z0-9_]{2,21}", 10)
    kws = _clean_list(body.get("keywords"), r"[^\"\n]{2,40}", 10)
    if not subs or not kws:
        abort(400, "Pick at least one subreddit and one keyword.")
    params = {
        "subreddits": subs, "keywords": kws,
        "time": body.get("time") if body.get("time") in TIME_FILTERS else "year",
        "limit": max(5, min(100, int(body.get("limit") or 25))),
        "comments": max(0, min(20, int(body.get("comments") or 5))),
    }
    run_id = f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
    d = RUNS / run_id
    d.mkdir()
    _write_meta(d, {"id": run_id, "created": _now(), "status": "queued", "params": params,
                    "grid": {}, "posts": 0, "usage": {}, "error": None})
    if not _start(d, scrape_first=True):
        shutil.rmtree(d)
        abort(409, "A run is already in progress. Wait for it to finish or stop it.")
    return jsonify(_read_meta(d)), 201


@app.get("/api/runs")
def list_runs():
    metas = [_read_meta(d) for d in RUNS.iterdir() if (d / "meta.json").exists()]
    metas.sort(key=lambda m: m["created"], reverse=True)
    return jsonify([{k: m.get(k) for k in ("id", "created", "status", "params", "posts", "quotes", "usage")}
                    for m in metas])


@app.get("/api/runs/<run_id>")
def get_run(run_id):
    d = _run_dir(run_id)
    meta = _read_meta(d)
    report = d / "report.md"
    meta["report_html"] = _render_report(report.read_text(encoding="utf-8")) if report.exists() else None
    meta["has_data"] = (d / "data.json").exists()
    return jsonify(meta)


@app.post("/api/runs/<run_id>/analyze")
def reanalyze(run_id):
    d = _run_dir(run_id)
    if not (d / "data.json").exists():
        abort(400, "This run has no scraped data to analyze.")
    if not _keys_status()["anthropic"]:
        abort(400, "Add your Anthropic API key in Settings first.")
    if not _start(d, scrape_first=False):
        abort(409, "A run is already in progress. Wait for it to finish or stop it.")
    return jsonify(ok=True)


@app.post("/api/runs/<run_id>/cancel")
def cancel_run(run_id):
    _run_dir(run_id)
    with _lock:
        if _active["id"] != run_id:
            abort(409, "This run isn't in progress.")
        _active["cancel"].set()
    return jsonify(ok=True)


@app.delete("/api/runs/<run_id>")
def delete_run(run_id):
    d = _run_dir(run_id)
    if _active["id"] == run_id:
        abort(409, "Stop the run before deleting it.")
    shutil.rmtree(d)
    return jsonify(ok=True)


@app.get("/api/runs/<run_id>/files/<name>")
def download(run_id, name):
    d = _run_dir(run_id)
    if name not in ("report.md", "data.json") or not (d / name).exists():
        abort(404)
    stamp = run_id[:8]
    nice = f"market_research_report_{stamp}.md" if name == "report.md" else f"reddit_pains_{stamp}.json"
    return send_file(d / name, as_attachment=True, download_name=nice)


if __name__ == "__main__":
    from waitress import serve
    _recover_interrupted()
    print(f"Pain Signal running at http://{HOST}:{PORT}  (Ctrl+C to stop)")
    serve(app, host=HOST, port=PORT, threads=8)
