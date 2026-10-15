#!/usr/bin/env python
"""Public gallery, photo-request inbox, queue board and telescope status board.

Dumb inbox + gallery: never interprets, executes, or forwards prompt text to any LLM.
Raw requests are shown only on /admin (owner, password) and via the authed API. /queue
shows only approved display_prompt text. Run locally: `python app.py` (waitress, $PORT).
"""
import functools
import json
import os
import secrets
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

from flask import Flask, abort, jsonify, render_template, request, redirect, send_from_directory, session, url_for
from PIL import Image
from werkzeug.utils import secure_filename

import db

SITE_TOKEN = os.environ.get("SITE_TOKEN")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")
SITE_TITLE = os.environ.get("SITE_TITLE", "Sky")
# Shown under the site name on every page.
SITE_SUBTITLE = os.environ.get("SITE_SUBTITLE", "Pictures from a Seestar S50 Pro.")
# Credited on pictures the owner asked for, as opposed to visitors or the planner.
OWNER_NAME = os.environ.get("OWNER_NAME", "Owner")
PORT = int(os.environ.get("PORT", "8090"))
TZ = ZoneInfo(os.environ.get("TIMEZONE", "UTC"))   # IANA name; all displayed times are local to this
MAX_REQUESTS_PER_IP_PER_DAY = 5
MAX_MOVES_PER_IP_PER_HOUR = 40
VOTER_COOKIE = "voter"
LIKES_PER_IP_PER_PHOTO = 5
ALLOWED_STATUSES = {"new", "accepted", "scheduled", "done", "rejected"}
QUEUE_STATUSES = ("accepted", "scheduled", "done")
OPEN_STATUSES = ("accepted", "scheduled")
PHOTO_FIELDS = ("title", "object", "kind", "taken", "frames", "sub", "total_exp", "filter", "gain", "notes", "credit")
REQUEST_FIELDS = ("display_prompt", "triage_note", "target", "source", "planned_for", "position")
# Older databases may hold a different owner token here; unknown values are accepted on
# read and only ever grouped as "not a visitor".
SOURCES = {"visitor", "ai", "owner"}
STALE_IMAGING_MIN, STALE_OTHER_MIN = 20, 90

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=os.environ.get("INSECURE_COOKIES") != "1",
                  PERMANENT_SESSION_LIFETIME=timedelta(days=30))

db.init_db()
_login_fails = {}   # ip -> [count, locked_until_epoch]
_moves = {}         # ip -> [timestamps]


# ---------- helpers ----------

def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def client_ip():
    """nginx sets X-Real-IP from the TCP peer; X-Forwarded-For[0] is client-controlled, so only its
    last element is a fallback. Port 8090 binds to 127.0.0.1, so everything comes through nginx."""
    real = request.headers.get("X-Real-IP", "").strip()
    if real:
        return real
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[-1].strip()
    return request.remote_addr or ""


def voter_id():
    return request.cookies.get(VOTER_COOKIE) or ""


def blank(s):
    return not (s or "").strip()


def pac(iso, fmt="%-I:%M %p"):
    """UTC/aware ISO string -> Pacific time text."""
    if not iso:
        return ""
    try:
        d = datetime.fromisoformat(iso)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(TZ).strftime(fmt)
    except Exception:
        return ""


def bump(conn):
    """Increment the change counter the pages poll."""
    conn.execute("INSERT INTO site_state (key, value, updated_at) VALUES ('version', '1', ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT), updated_at = excluded.updated_at",
                 (now_iso(),))


def get_state(conn, key):
    row = conn.execute("SELECT value, updated_at FROM site_state WHERE key = ?", (key,)).fetchone()
    return (row["value"], row["updated_at"]) if row else (None, None)


def status_view(conn):
    """The scope status as the banner shows it. Stale statuses degrade to 'unknown', never to stale 'photographing'."""
    raw_, updated = get_state(conn, "status")
    if not raw_:
        return {"cls": "unknown", "text": "Telescope status unknown", "when": ""}
    try:
        st = json.loads(raw_)
    except Exception:
        return {"cls": "unknown", "text": "Telescope status unknown", "when": ""}
    state = st.get("state", "unknown"); detail = st.get("detail") or ""
    age_min = 1e9
    try:
        age_min = (datetime.now(timezone.utc) - datetime.fromisoformat(updated)).total_seconds() / 60
    except Exception:
        pass
    limit = STALE_IMAGING_MIN if state in ("imaging", "processing", "clouded") else STALE_OTHER_MIN
    if age_min > limit:
        return {"cls": "unknown", "text": "Telescope status unknown", "when": f"last heard from it at {pac(updated)}"}
    since = pac(st.get("since"))
    if state == "imaging":
        when = f"since {since}" if since else ""
        if st.get("minutes") is not None and st.get("planned"):
            when += f" · {st['minutes']} min of {st['planned']}"
        return {"cls": "imaging", "text": f"Photographing {detail} now", "when": when}
    if state == "processing":
        return {"cls": "imaging", "text": f"Making the picture: {detail}", "when": ""}
    if state == "clouded":
        return {"cls": "clouded", "text": "Clouded over. Waiting for the sky to clear, checking every half hour", "when": f"since {since}" if since else ""}
    if state == "daylight":
        return {"cls": "daylight", "text": f"Daytime. {detail[0].upper() + detail[1:] if detail else 'The telescope wakes at dusk'}", "when": ""}
    if state == "offline":
        return {"cls": "offline", "text": "Telescope offline" + (f": {detail}" if detail else ""), "when": f"last heard from it at {pac(updated)}"}
    return {"cls": "idle", "text": (detail[0].upper() + detail[1:]) if detail else "Telescope idle", "when": ""}


def notice_view(conn):
    """A standing notice pinned above everything - set when the scope is out of action.

    Stored in site_state under 'notice' as JSON {"heading":..., "body":..., "cls":...};
    absent or blank means no notice. Unlike `status`, a notice never expires on its own -
    it stays until it is explicitly cleared, because the thing it describes (a broken
    telescope) does not fix itself.
    """
    raw_, _ = get_state(conn, "notice")
    if not raw_:
        return None
    try:
        n = json.loads(raw_)
    except Exception:
        return {"heading": "", "body": raw_, "cls": "warn"}
    if not (n.get("body") or n.get("heading")):
        return None
    return {"heading": n.get("heading") or "", "body": n.get("body") or "", "cls": n.get("cls") or "warn"}


def gallery_items(conn):
    vid = voter_id()
    rows = conn.execute("""
        SELECT p.*, r.id AS req_id, r.submitter_name AS submitter_name,
            (SELECT COUNT(*) FROM likes l WHERE l.photo_id = p.id) AS likes,
            (SELECT COUNT(*) FROM likes l WHERE l.photo_id = p.id AND l.voter_id = ?) AS liked
        FROM photos p LEFT JOIN requests r ON r.id = p.request_id
        WHERE p.featured = 1 ORDER BY p.taken DESC, p.id DESC""", (vid,)).fetchall()
    return [dict(r) for r in rows]


@app.context_processor
def inject_site():
    conn = db.get_db()
    try:
        sv = status_view(conn); ver = get_state(conn, "version")[0] or "0"
        nv = notice_view(conn)
    finally:
        conn.close()
    return {"site_title": SITE_TITLE, "site_subtitle": SITE_SUBTITLE, "owner_name": OWNER_NAME,
            "status": sv, "notice": nv, "version": ver,
            "is_admin": bool(session.get("admin"))}


# ---------- auth ----------

def require_token(fn):
    @functools.wraps(fn)
    def wrapped(*a, **kw):
        auth = request.headers.get("Authorization", "")
        expected = f"Bearer {SITE_TOKEN}" if SITE_TOKEN else ""
        if not SITE_TOKEN or not secrets.compare_digest(auth.encode(), expected.encode()):
            return jsonify(error="unauthorized"), 401
        return fn(*a, **kw)
    return wrapped


def require_admin(fn):
    @functools.wraps(fn)
    def wrapped(*a, **kw):
        if not session.get("admin"):
            return redirect(url_for("admin_login", next=request.path))
        return fn(*a, **kw)
    return wrapped


# ---------- public pages ----------

@app.route("/")
def gallery():
    conn = db.get_db()
    items = gallery_items(conn)
    conn.close()
    return render_template("gallery.html", items=items, active="gallery")


@app.route("/sw.js")
def service_worker():
    """Served from the root so the app's service worker covers the whole site."""
    r = app.send_static_file("sw.js")
    r.headers["Cache-Control"] = "no-cache"
    r.headers["Content-Type"] = "application/javascript"
    return r


@app.route("/how")
def how():
    return render_template("how.html", active="how")


@app.route("/gallery.json")
def gallery_json():
    conn = db.get_db()
    items = gallery_items(conn)
    conn.close()
    return jsonify(items)


@app.route("/state")
def state():
    conn = db.get_db()
    try:
        return jsonify(version=get_state(conn, "version")[0] or "0",
                       status=status_view(conn), notice=notice_view(conn))
    finally:
        conn.close()


@app.route("/like/<path:filename>", methods=["POST"])
def like(filename):
    ip = client_ip(); vid = voter_id(); set_cookie = None
    if not vid:
        vid = secrets.token_urlsafe(16); set_cookie = vid
    conn = db.get_db()
    photo = conn.execute("SELECT id FROM photos WHERE filename = ?", (filename,)).fetchone()
    if photo is None:
        conn.close(); return jsonify(error="not found"), 404
    pid = photo["id"]
    existing = conn.execute("SELECT id FROM likes WHERE photo_id = ? AND voter_id = ?", (pid, vid)).fetchone()
    capped = False
    if existing:
        conn.execute("DELETE FROM likes WHERE id = ?", (existing["id"],)); liked = False
    else:
        ip_count = conn.execute("SELECT COUNT(*) FROM likes WHERE photo_id = ? AND ip = ?", (pid, ip)).fetchone()[0]
        if ip and ip_count >= LIKES_PER_IP_PER_PHOTO:
            liked, capped = False, True
        else:
            conn.execute("INSERT OR IGNORE INTO likes (photo_id, voter_id, ip, created_at) VALUES (?, ?, ?, ?)", (pid, vid, ip, now_iso())); liked = True
    conn.commit()
    count = conn.execute("SELECT COUNT(*) FROM likes WHERE photo_id = ?", (pid,)).fetchone()[0]
    conn.close()
    resp = jsonify(ok=True, likes=count, liked=liked, capped=capped)
    if set_cookie:
        resp.set_cookie(VOTER_COOKIE, set_cookie, max_age=60 * 60 * 24 * 365 * 2, samesite="Lax", secure=True, httponly=True)
    return resp


@app.route("/request", methods=["GET", "POST"])
def request_form():
    if request.method == "GET":
        return render_template("request.html", active="request")
    name = (request.form.get("name") or "").strip()[:80]
    prompt = (request.form.get("prompt") or "").strip()[:500]
    if (request.form.get("website") or "").strip():
        return render_template("request.html", active="request", name=name, prompt=prompt, error="Could not submit your request."), 400
    if not prompt:
        return render_template("request.html", active="request", name=name, prompt=prompt, error="Please describe what you'd like to see."), 400
    ip = client_ip(); conn = db.get_db()
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
    count = conn.execute("SELECT COUNT(*) FROM requests WHERE ip = ? AND created_at >= ?", (ip, cutoff)).fetchone()[0]
    if count >= MAX_REQUESTS_PER_IP_PER_DAY:
        conn.close()
        return render_template("request.html", active="request", name=name, prompt=prompt, error="You've hit the daily limit for requests. Try again tomorrow."), 429
    token = secrets.token_urlsafe(16); ts = now_iso()
    conn.execute("INSERT INTO requests (token, created_at, ip, submitter_name, prompt_text, status, updated_at, source) VALUES (?, ?, ?, ?, ?, 'new', ?, 'visitor')",
                 (token, ts, ip, name or None, prompt, ts))
    bump(conn); conn.commit(); conn.close()
    return redirect(url_for("status", token=token))


def queue_rows(conn):
    """Three groups: visitor requests in arrival order, then AI/owner picks by position (with the
    tonight/next divider), then finished ones. Only approved wording is ever included."""
    rows = conn.execute("""
        SELECT r.*, p.filename AS p_filename, p.title AS p_title
        FROM requests r LEFT JOIN photos p ON p.id = r.photo_id
        WHERE r.status IN ('accepted','scheduled','done') AND COALESCE(TRIM(r.display_prompt), '') <> ''
        ORDER BY r.id""").fetchall()
    def pub(r):
        return {"id": r["id"], "status": r["status"], "display_prompt": r["display_prompt"], "who": r["submitter_name"] or ("AI" if r["source"] == "ai" else "Anonymous"),
                "source": r["source"], "planned_for": r["planned_for"], "position": r["position"], "updated_at": r["updated_at"],
                "photo": {"filename": r["p_filename"], "title": r["p_title"]} if r["p_filename"] else None}
    visitors = [pub(r) for r in rows if r["status"] in OPEN_STATUSES and r["source"] == "visitor"]
    picks = sorted([pub(r) for r in rows if r["status"] in OPEN_STATUSES and r["source"] != "visitor"], key=lambda x: (x["position"] is None, x["position"] or 0, x["id"]))
    done = sorted([pub(r) for r in rows if r["status"] == "done"], key=lambda x: x["updated_at"] or "", reverse=True)[:30]
    return visitors, picks, done


@app.route("/queue")
def queue():
    conn = db.get_db()
    visitors, picks, done = queue_rows(conn)
    conn.close()
    return render_template("queue.html", visitors=visitors, picks=picks, done=done, active="queue")


@app.route("/queue/move", methods=["POST"])
def queue_move():
    """Anyone may reorder the telescope's own picks (never visitor requests). Swap with the neighbour."""
    ip = client_ip(); t = time.time()
    hist = [x for x in _moves.get(ip, []) if t - x < 3600]
    if len(hist) >= MAX_MOVES_PER_IP_PER_HOUR:
        return jsonify(error="too many moves, try later"), 429
    body = request.get_json(silent=True) or {}
    try:
        rid = int(body.get("id")); direction = body.get("dir")
    except Exception:
        return jsonify(error="bad request"), 400
    if direction not in ("up", "down"):
        return jsonify(error="bad request"), 400
    conn = db.get_db()
    _, picks, _ = queue_rows(conn)
    ids = [p["id"] for p in picks]
    if rid not in ids:
        conn.close(); return jsonify(error="not movable"), 400
    i = ids.index(rid); j = i - 1 if direction == "up" else i + 1
    if j < 0 or j >= len(ids):
        conn.close(); return jsonify(ok=True, moved=False)
    ids[i], ids[j] = ids[j], ids[i]
    ts = now_iso()
    for pos, pid in enumerate(ids, start=1):
        conn.execute("UPDATE requests SET position = ?, updated_at = ? WHERE id = ?", (pos, ts, pid))
    bump(conn); conn.commit(); conn.close()
    hist.append(t); _moves[ip] = hist
    return jsonify(ok=True, moved=True)


@app.route("/r/<token>")
def status(token):
    conn = db.get_db()
    req = conn.execute("SELECT * FROM requests WHERE token = ?", (token,)).fetchone()
    if req is None:
        conn.close(); abort(404)
    photo = conn.execute("SELECT * FROM photos WHERE id = ?", (req["photo_id"],)).fetchone() if req["status"] == "done" and req["photo_id"] else None
    conn.close()
    return render_template("status.html", req=req, photo=photo, active="")


@app.route("/healthz")
def healthz():
    return "ok", 200


@app.route("/full/<path:filename>")
def full_file(filename):
    return send_from_directory(db.DATA_DIR / "full", filename)


@app.route("/thumb/<path:filename>")
def thumb_file(filename):
    return send_from_directory(db.DATA_DIR / "thumb", filename)


# ---------- admin (owner only; a dumb form, no LLM anywhere) ----------

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    ip = client_ip(); fails = _login_fails.get(ip, [0, 0])
    if request.method == "GET":
        return render_template("admin_login.html", active="", locked=time.time() < fails[1])
    if time.time() < fails[1]:
        return render_template("admin_login.html", active="", locked=True), 429
    pw = request.form.get("password") or ""
    if ADMIN_PASSWORD and secrets.compare_digest(pw.encode(), ADMIN_PASSWORD.encode()):
        session.permanent = True; session["admin"] = True; _login_fails.pop(ip, None)
        nxt = request.args.get("next") or url_for("admin")
        return redirect(nxt if nxt.startswith("/") else url_for("admin"))
    fails[0] += 1
    if fails[0] >= 5:
        fails = [0, time.time() + 3600]
    _login_fails[ip] = fails
    return render_template("admin_login.html", active="", error="Wrong password.", locked=fails[1] > time.time()), 401


@app.route("/admin/logout", methods=["POST"])
def admin_logout():
    session.clear(); return redirect(url_for("gallery"))


@app.route("/admin")
@require_admin
def admin():
    conn = db.get_db()
    new = [dict(r) for r in conn.execute("SELECT * FROM requests WHERE status = 'new' ORDER BY created_at").fetchall()]
    for r in new:
        r["when"] = pac(r["created_at"], "%b %-d, %-I:%M %p")
    visitors, picks, done = queue_rows(conn)
    photos = [dict(r) for r in conn.execute("SELECT * FROM photos ORDER BY taken DESC, id DESC").fetchall()]
    raw_, updated = get_state(conn, "status")
    try:
        st = json.loads(raw_) if raw_ else {}
    except Exception:
        st = {}
    st["updated_pac"] = pac(updated, "%b %-d, %-I:%M %p"); st["since_pac"] = pac(st.get("since"))
    conn.close()
    return render_template("admin.html", new=new, visitors=visitors, picks=picks, done=done, photos=photos, st=st, active="")


@app.route("/admin/request/<int:req_id>", methods=["POST"])
@require_admin
def admin_request(req_id):
    action = request.form.get("action"); conn = db.get_db()
    row = conn.execute("SELECT * FROM requests WHERE id = ?", (req_id,)).fetchone()
    if row is None:
        conn.close(); abort(404)
    ts = now_iso()
    if action == "accept":
        target = (request.form.get("target") or "").strip()[:120]; wording = (request.form.get("display_prompt") or "").strip()[:500]
        if not target or not wording:
            conn.close(); return redirect(url_for("admin") + "#req-" + str(req_id))
        conn.execute("UPDATE requests SET status = 'scheduled', target = ?, display_prompt = ?, updated_at = ? WHERE id = ?", (target, wording, ts, req_id))
    elif action == "reject":
        note = (request.form.get("triage_note") or "").strip()[:300] or None
        conn.execute("UPDATE requests SET status = 'rejected', triage_note = ?, updated_at = ? WHERE id = ?", (note, ts, req_id))
    bump(conn); conn.commit(); conn.close()
    return redirect(url_for("admin"))


@app.route("/admin/photo/<int:photo_id>", methods=["POST"])
@require_admin
def admin_photo(photo_id):
    conn = db.get_db()
    if conn.execute("SELECT 1 FROM photos WHERE id = ?", (photo_id,)).fetchone() is None:
        conn.close(); abort(404)
    fields, values = [], []
    if "featured" in request.form:
        fields.append("featured = ?"); values.append(1 if request.form.get("featured") == "1" else 0)
    if request.form.get("credit") in ("AI", OWNER_NAME):
        fields.append("credit = ?"); values.append(request.form.get("credit"))
    if fields:
        values.append(photo_id)
        conn.execute(f"UPDATE photos SET {', '.join(fields)} WHERE id = ?", values); bump(conn); conn.commit()
    conn.close()
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify(ok=True)
    return redirect(url_for("admin") + "#photos")


# ---------- private API (bearer token) ----------

@app.route("/api/notice", methods=["POST"])
@require_token
def api_notice():
    """Set or clear the standing notice.

    POST {"heading": "...", "body": "...", "cls": "warn"|"down"|"info"} to set it;
    POST {} or {"body": ""} to clear it. Notices do not expire - clear it deliberately.
    """
    body = request.get_json(silent=True) or {}
    conn = db.get_db()
    text = (body.get("body") or "").strip()
    heading = (body.get("heading") or "").strip()
    if not text and not heading:
        conn.execute("DELETE FROM site_state WHERE key = 'notice'")
    else:
        payload = json.dumps({"heading": heading, "body": text,
                              "cls": body.get("cls") or "warn"})
        conn.execute("INSERT INTO site_state (key, value, updated_at) VALUES ('notice', ?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                     (payload, now_iso()))
    bump(conn); conn.commit()
    out = notice_view(conn); conn.close()
    return jsonify(ok=True, notice=out)


# ---------- live scope panel (admin page <-> agent on the observing machine) ----------
# The site never reaches the telescope. The agent next to the scope polls /api/scope/cmd,
# and while a live window is open it posts readings to /api/scope/live. All of it lives in
# site_state: scope_cmd (what the owner asked for), scope_live (latest readings),
# scope_pa (last polar-alignment measurement).
LIVE_WINDOW_MIN = 10


def _state_json(conn, key):
    raw_, updated = get_state(conn, key)
    try:
        return (json.loads(raw_) if raw_ else None), updated
    except Exception:
        return None, updated


def _put_state(conn, key, value):
    conn.execute("INSERT INTO site_state (key, value, updated_at) VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                 (key, json.dumps(value), now_iso()))


def scope_panel(conn):
    cmd, _ = _state_json(conn, "scope_cmd")
    live, live_at = _state_json(conn, "scope_live")
    pa, pa_at = _state_json(conn, "scope_pa")
    now = time.time()
    cmd = cmd or {}
    live_until = cmd.get("live_until") or 0
    return {"now": now, "live_until": live_until, "live_on": live_until > now,
            "polar_requested": bool(cmd.get("polar")), "live": live, "live_at": live_at,
            "live_age": (now - live["ts"]) if live and live.get("ts") else None,
            "pa": pa, "pa_at_pac": pac(pa_at, "%-I:%M %p") if pa_at else None}


@app.route("/admin/scope")
@require_admin
def admin_scope():
    return render_template("scope.html", active="admin", window_min=LIVE_WINDOW_MIN)


@app.route("/admin/scope.json")
@require_admin
def admin_scope_json():
    conn = db.get_db()
    try:
        return jsonify(scope_panel(conn))
    finally:
        conn.close()


@app.route("/admin/scope/cmd", methods=["POST"])
@require_admin
def admin_scope_cmd():
    what = request.form.get("cmd") or (request.get_json(silent=True) or {}).get("cmd")
    conn = db.get_db()
    cmd, _ = _state_json(conn, "scope_cmd"); cmd = cmd or {}
    if what == "live":
        cmd["live_until"] = time.time() + LIVE_WINDOW_MIN * 60
    elif what == "stop":
        cmd["live_until"] = 0; cmd["polar"] = False
    elif what == "polar":
        cmd["polar"] = True
        cmd["live_until"] = max(cmd.get("live_until") or 0, time.time() + 30 * 60)   # adjusting takes a while
    else:
        conn.close(); return jsonify(error="bad cmd"), 400
    cmd["requested_at"] = now_iso()
    _put_state(conn, "scope_cmd", cmd); conn.commit()
    out = scope_panel(conn); conn.close()
    return jsonify(out)


@app.route("/api/scope/cmd")
@require_token
def api_scope_cmd():
    conn = db.get_db()
    try:
        cmd, _ = _state_json(conn, "scope_cmd"); cmd = cmd or {}
        return jsonify(live_until=cmd.get("live_until") or 0, polar=bool(cmd.get("polar")), now=time.time())
    finally:
        conn.close()


@app.route("/api/scope/cmd/polar-taken", methods=["POST"])
@require_token
def api_scope_polar_taken():
    conn = db.get_db()
    cmd, _ = _state_json(conn, "scope_cmd"); cmd = cmd or {}
    cmd["polar"] = False
    _put_state(conn, "scope_cmd", cmd); conn.commit(); conn.close()
    return jsonify(ok=True)


@app.route("/api/scope/live", methods=["POST"])
@require_token
def api_scope_live():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify(error="json object required"), 400
    body["ts"] = time.time()
    conn = db.get_db()
    _put_state(conn, "scope_live", body); conn.commit(); conn.close()
    return jsonify(ok=True)


@app.route("/api/scope/pa", methods=["POST"])
@require_token
def api_scope_pa():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify(error="json object required"), 400
    conn = db.get_db()
    _put_state(conn, "scope_pa", body); conn.commit(); conn.close()
    return jsonify(ok=True)


@app.route("/api/status", methods=["POST"])
@require_token
def api_status():
    body = request.get_json(silent=True) or {}
    if body.get("state") not in ("imaging", "processing", "clouded", "daylight", "offline", "idle"):
        return jsonify(error="bad state"), 400
    body.setdefault("since", now_iso())
    conn = db.get_db()
    conn.execute("INSERT INTO site_state (key, value, updated_at) VALUES ('status', ?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                 (json.dumps(body), now_iso()))
    bump(conn); conn.commit(); conn.close()
    return jsonify(ok=True)


@app.route("/api/requests")
@require_token
def api_requests():
    status_filter = request.args.get("status", "new"); conn = db.get_db()
    cols = "id, created_at, submitter_name, prompt_text, display_prompt, triage_note, status, photo_id, updated_at, target, source, planned_for, position"
    if status_filter == "all":
        rows = conn.execute(f"SELECT {cols} FROM requests ORDER BY created_at DESC").fetchall()
    else:
        rows = conn.execute(f"SELECT {cols} FROM requests WHERE status = ? ORDER BY created_at DESC", (status_filter,)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/requests", methods=["POST"])
@require_token
def api_request_create():
    """The private side files the telescope's own picks so they appear on /queue as requests."""
    body = request.get_json(silent=True) or {}
    status_ = body.get("status", "scheduled"); source = body.get("source", "ai")
    if status_ not in ALLOWED_STATUSES or source not in SOURCES:
        return jsonify(error="bad status or source"), 400
    if status_ in QUEUE_STATUSES and blank(body.get("display_prompt")):
        return jsonify(error="display_prompt required"), 400
    conn = db.get_db(); ts = now_iso()
    pos = (conn.execute("SELECT COALESCE(MAX(position), 0) FROM requests WHERE source <> 'visitor' AND status IN ('accepted','scheduled')").fetchone()[0] or 0) + 1
    cur = conn.execute("INSERT INTO requests (token, created_at, ip, submitter_name, prompt_text, display_prompt, status, updated_at, target, source, planned_for, position) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                       (secrets.token_urlsafe(16), ts, None, body.get("submitter_name") or ("AI" if source == "ai" else OWNER_NAME), body.get("prompt_text") or body.get("display_prompt"),
                        (body.get("display_prompt") or "").strip() or None, status_, ts, body.get("target"), source, body.get("planned_for"), body.get("position", pos)))
    bump(conn); conn.commit()
    row = conn.execute("SELECT * FROM requests WHERE id = ?", (cur.lastrowid,)).fetchone(); conn.close()
    return jsonify(ok=True, request=dict(row))


def _photo_exists(conn, photo_id):
    return isinstance(photo_id, int) and not isinstance(photo_id, bool) and conn.execute("SELECT 1 FROM photos WHERE id = ?", (photo_id,)).fetchone() is not None


@app.route("/api/requests/<int:req_id>", methods=["PATCH"])
@require_token
def api_request_patch(req_id):
    body = request.get_json(silent=True) or {}; conn = db.get_db()
    row = conn.execute("SELECT * FROM requests WHERE id = ?", (req_id,)).fetchone()
    if row is None:
        conn.close(); return jsonify(error="not found"), 404
    fields, values = [], []; new_status = row["status"]
    if "status" in body:
        new_status = body["status"]
        if new_status not in ALLOWED_STATUSES:
            conn.close(); return jsonify(error="bad status"), 400
        fields.append("status = ?"); values.append(new_status)
    display_prompt = row["display_prompt"]
    if "display_prompt" in body:
        display_prompt = (body["display_prompt"] or "").strip() or None
        fields.append("display_prompt = ?"); values.append(display_prompt)
    if new_status in QUEUE_STATUSES and blank(display_prompt):
        conn.close(); return jsonify(error="display_prompt required: accepted/scheduled/done requests are shown publicly and need approved wording"), 400
    for k in ("triage_note", "target", "planned_for", "position"):
        if k in body:
            fields.append(f"{k} = ?"); values.append(body[k])
    if "source" in body:
        if body["source"] not in SOURCES:
            conn.close(); return jsonify(error="bad source"), 400
        fields.append("source = ?"); values.append(body["source"])
    if "photo_id" in body:
        pid = body["photo_id"]
        if pid is not None and not _photo_exists(conn, pid):
            conn.close(); return jsonify(error="unknown photo_id"), 400
        fields.append("photo_id = ?"); values.append(pid)
    if not fields:
        conn.close(); return jsonify(error="no fields to update"), 400
    fields.append("updated_at = ?"); values.append(now_iso()); values.append(req_id)
    conn.execute(f"UPDATE requests SET {', '.join(fields)} WHERE id = ?", values)
    bump(conn); conn.commit()
    updated = conn.execute("SELECT * FROM requests WHERE id = ?", (req_id,)).fetchone(); conn.close()
    return jsonify(ok=True, request=dict(updated))


@app.route("/api/photos")
@require_token
def api_photos_list():
    conn = db.get_db()
    rows = conn.execute("SELECT p.*, (SELECT COUNT(*) FROM likes l WHERE l.photo_id = p.id) AS likes FROM photos p ORDER BY p.taken DESC, p.id DESC").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/photos", methods=["POST"])
@require_token
def api_photos():
    if "file" not in request.files or not request.files["file"].filename:
        return jsonify(error="missing file"), 400
    file = request.files["file"]
    try:
        meta = json.loads(request.form.get("meta") or "{}")
    except ValueError:
        return jsonify(error="bad meta json"), 400
    safe = secure_filename(file.filename)
    if not safe:
        return jsonify(error="bad filename"), 400
    conn = db.get_db()
    request_id = meta.get("request_id")
    if request_id is not None:
        if isinstance(request_id, bool) or not isinstance(request_id, int):
            conn.close(); return jsonify(error="request_id must be an integer"), 400
        req_row = conn.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone()
        if req_row is None:
            conn.close(); return jsonify(error="unknown request_id"), 400
        if blank(req_row["display_prompt"]):
            conn.close(); return jsonify(error="request has no display_prompt; set approved wording before attaching a photo"), 400
    full_path = db.DATA_DIR / "full" / safe; stem, ext = os.path.splitext(safe); n = 1
    while full_path.exists():
        safe = f"{stem}-{n}{ext}"; full_path = db.DATA_DIR / "full" / safe; n += 1
    file.save(full_path)
    try:
        img = Image.open(full_path); img.load()
    except Exception:
        full_path.unlink(missing_ok=True); conn.close(); return jsonify(error="bad image"), 400
    width, height = img.size
    thumb_img = img.convert("RGB") if img.mode not in ("RGB", "L") else img.copy()
    if width > 640:
        thumb_img = thumb_img.resize((640, max(1, round(height * 640 / width))), Image.LANCZOS)
    thumb_img.save(db.DATA_DIR / "thumb" / safe, quality=86)
    featured = 1 if meta.get("featured", True) else 0; ts = now_iso()
    cur = conn.execute("INSERT INTO photos (filename, title, object, kind, taken, frames, sub, total_exp, filter, gain, notes, credit, request_id, created_at, width, height, bytes, featured) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (safe, meta.get("title"), meta.get("object"), meta.get("kind"), meta.get("taken"), meta.get("frames"), meta.get("sub"), meta.get("total_exp"),
                        meta.get("filter"), meta.get("gain"), meta.get("notes"), meta.get("credit") or "AI", request_id, ts, width, height, full_path.stat().st_size, featured))
    photo_id = cur.lastrowid
    if request_id is not None:
        conn.execute("UPDATE requests SET status = 'done', photo_id = ?, updated_at = ? WHERE id = ?", (photo_id, ts, request_id))
    bump(conn); conn.commit(); conn.close()
    return jsonify(ok=True, id=photo_id, filename=safe)


@app.route("/api/photos/<int:photo_id>/file", methods=["PUT"])
@require_token
def api_photo_replace_file(photo_id):
    """Replace the stored image (and thumbnail) of an existing photo, keeping its id, filename and
    metadata. For re-rendered pictures and corrected site-survey sheets."""
    if "file" not in request.files or not request.files["file"].filename:
        return jsonify(error="missing file"), 400
    conn = db.get_db()
    row = conn.execute("SELECT * FROM photos WHERE id = ?", (photo_id,)).fetchone()
    if row is None:
        conn.close(); return jsonify(error="not found"), 404
    tmp = db.DATA_DIR / "full" / (row["filename"] + ".new")
    request.files["file"].save(tmp)
    try:
        img = Image.open(tmp); img.load()
    except Exception:
        tmp.unlink(missing_ok=True); conn.close(); return jsonify(error="bad image"), 400
    full_path = db.DATA_DIR / "full" / row["filename"]; tmp.replace(full_path)
    width, height = img.size
    thumb_img = img.convert("RGB") if img.mode not in ("RGB", "L") else img.copy()
    if width > 640:
        thumb_img = thumb_img.resize((640, max(1, round(height * 640 / width))), Image.LANCZOS)
    thumb_img.save(db.DATA_DIR / "thumb" / row["filename"], quality=86)
    conn.execute("UPDATE photos SET width = ?, height = ?, bytes = ? WHERE id = ?", (width, height, full_path.stat().st_size, photo_id))
    bump(conn); conn.commit(); conn.close()
    return jsonify(ok=True, id=photo_id, width=width, height=height)


@app.route("/api/photos/<int:photo_id>", methods=["PATCH"])
@require_token
def api_photo_patch(photo_id):
    body = request.get_json(silent=True) or {}; conn = db.get_db()
    if conn.execute("SELECT 1 FROM photos WHERE id = ?", (photo_id,)).fetchone() is None:
        conn.close(); return jsonify(error="not found"), 404
    fields, values = [], []
    for k in PHOTO_FIELDS:
        if k in body:
            fields.append(f"{k} = ?"); values.append(body[k])
    if "featured" in body:
        fields.append("featured = ?"); values.append(1 if body["featured"] else 0)
    if "request_id" in body:
        rid = body["request_id"]
        if rid is not None and (isinstance(rid, bool) or not isinstance(rid, int) or conn.execute("SELECT 1 FROM requests WHERE id = ?", (rid,)).fetchone() is None):
            conn.close(); return jsonify(error="unknown request_id"), 400
        fields.append("request_id = ?"); values.append(rid)
    if not fields:
        conn.close(); return jsonify(error="no fields to update"), 400
    values.append(photo_id)
    conn.execute(f"UPDATE photos SET {', '.join(fields)} WHERE id = ?", values); bump(conn); conn.commit()
    updated = conn.execute("SELECT * FROM photos WHERE id = ?", (photo_id,)).fetchone(); conn.close()
    return jsonify(ok=True, photo=dict(updated))


if __name__ == "__main__":
    if not SITE_TOKEN:
        raise SystemExit("SITE_TOKEN is required (set it in .env)")
    from waitress import serve
    print(f"serving on :{PORT}")
    serve(app, host="0.0.0.0", port=PORT, threads=8)
