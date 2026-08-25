#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
flask_app.py — Instagram Comment-to-DM Automation Engine
==========================================================

A single-file, production-ready Flask application that automates replying
to Instagram comments with a follower-aware direct message, built to run
reliably on PythonAnywhere's free tier (no Celery, no Redis, no Docker).

DEPLOY NOTES (read before going live)
--------------------------------------
1. PythonAnywhere free accounts only allow outbound HTTPS requests to a
   whitelist of domains. `graph.facebook.com` is on that whitelist, so
   this works on the free tier — but if Meta ever changes API hosts,
   check Pythonanywhere's "Whitelisted Sites" page under the Account tab.
2. Set your Web app's WSGI file to import this module:
       from flask_app import app as application
3. Put this file (and the auto-created `automation.db` SQLite file) in
   the same directory PythonAnywhere points your WSGI config at.
4. In the Instagram/Meta App dashboard, set the Webhook Callback URL to:
       https://<your-username>.pythonanywhere.com/webhook
   and the Verify Token to the value shown in Settings → Verify Token
   inside this app's dashboard (auto-generated on first run).
5. Subscribe your app to the `comments` field for your connected Page.

CONFIGURATION
-------------
Everything below can be overridden with environment variables on first
boot (handy for PythonAnywhere's "Environment variables" section), but
all of it is also editable live from the dashboard's Settings tab and
persisted in SQLite — env vars only *seed* the database once.
"""

import os
import json
import time
import sqlite3
import secrets
import logging
import threading
from datetime import timedelta
from functools import wraps

import requests
from flask import Flask, request, jsonify, session, redirect, g, Response
from werkzeug.security import generate_password_hash, check_password_hash

# ---------------------------------------------------------------------------
# Inline configuration (env-var overridable, only used to SEED the DB once)
# ---------------------------------------------------------------------------
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "")           # webhook verify token
ACCESS_TOKEN = os.environ.get("ACCESS_TOKEN", "")           # long-lived Page access token
IG_APP_ID = os.environ.get("IG_APP_ID", "")                 # Meta App ID
IG_APP_SECRET = os.environ.get("IG_APP_SECRET", "")         # Meta App Secret
SECRET_KEY = os.environ.get("SECRET_KEY", "")                # Flask session secret
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "")       # optional auto-setup
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")       # optional auto-setup

GRAPH_API_VERSION = "v21.0"
GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "automation.db")
SESSION_LIFETIME_DAYS = 14

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("commentdm")

# ---------------------------------------------------------------------------
# Database schema
# ---------------------------------------------------------------------------
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS automations (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    name                  TEXT NOT NULL,
    target_media          TEXT NOT NULL DEFAULT 'ALL',
    target_media_caption  TEXT DEFAULT '',
    target_media_thumb    TEXT DEFAULT '',
    trigger_type          TEXT NOT NULL DEFAULT 'ALL',
    keywords              TEXT DEFAULT '',
    message_follower      TEXT NOT NULL DEFAULT '',
    message_nonfollower   TEXT NOT NULL DEFAULT '',
    public_reply_enabled  INTEGER NOT NULL DEFAULT 0,
    public_reply_text     TEXT DEFAULT '',
    active                INTEGER NOT NULL DEFAULT 1,
    created_at            TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS processed_comments (
    comment_id           TEXT PRIMARY KEY,
    automation_id        INTEGER,
    media_id             TEXT,
    commenter_id         TEXT,
    commenter_username   TEXT,
    comment_text         TEXT,
    is_follower          INTEGER,
    dm_status            TEXT DEFAULT 'processing',
    error_message        TEXT,
    created_at            TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS event_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    level       TEXT,
    event_type  TEXT,
    message     TEXT,
    meta        TEXT,
    created_at  TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_processed_status ON processed_comments(dm_status);
CREATE INDEX IF NOT EXISTS idx_processed_created ON processed_comments(created_at);
CREATE INDEX IF NOT EXISTS idx_events_type ON event_logs(event_type);
"""


def get_raw_conn():
    """A standalone SQLite connection for use outside Flask's app/request
    context (module bootstrap, background worker threads)."""
    conn = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db():
    conn = get_raw_conn()
    conn.executescript(SCHEMA_SQL)
    conn.commit()

    defaults = {
        "secret_key": SECRET_KEY or secrets.token_hex(32),
        "verify_token": VERIFY_TOKEN or secrets.token_hex(16),
        "ig_app_id": IG_APP_ID,
        "ig_app_secret": IG_APP_SECRET,
        "access_token": ACCESS_TOKEN,
        "page_id": "",
        "ig_user_id": "",
        "ig_username": "",
        "ig_profile_pic": "",
        "setup_complete": "0",
        "admin_username": "",
        "admin_password_hash": "",
        "last_webhook_at": "",
        "last_token_check": "",
        "last_token_valid": "0",
    }
    for k, v in defaults.items():
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))
    conn.commit()

    # Optional zero-touch admin bootstrap via env vars
    row = conn.execute("SELECT value FROM settings WHERE key='setup_complete'").fetchone()
    if row and row["value"] == "0" and ADMIN_USERNAME and ADMIN_PASSWORD:
        conn.execute("UPDATE settings SET value=? WHERE key='admin_username'", (ADMIN_USERNAME,))
        conn.execute(
            "UPDATE settings SET value=? WHERE key='admin_password_hash'",
            (generate_password_hash(ADMIN_PASSWORD),),
        )
        conn.execute("UPDATE settings SET value='1' WHERE key='setup_complete'")
        conn.commit()

    conn.close()


init_db()


def _bootstrap_secret_key():
    conn = get_raw_conn()
    row = conn.execute("SELECT value FROM settings WHERE key='secret_key'").fetchone()
    conn.close()
    return row["value"] if row and row["value"] else secrets.token_hex(32)


# ---------------------------------------------------------------------------
# Flask app + per-request DB handle
# ---------------------------------------------------------------------------
app = Flask(__name__)
app.secret_key = _bootstrap_secret_key()
app.permanent_session_lifetime = timedelta(days=SESSION_LIFETIME_DAYS)


def get_db():
    if "db" not in g:
        g.db = get_raw_conn()
    return g.db


@app.teardown_appcontext
def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def get_setting(key, default=""):
    row = get_db().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row and row["value"] is not None else default


def set_setting(key, value):
    db = get_db()
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    db.commit()


def get_all_settings(conn=None):
    conn = conn or get_db()
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    return {r["key"]: r["value"] for r in rows}


def log_event(level, event_type, message, meta=None):
    """Fire-and-forget event log write. Never raises."""
    try:
        conn = get_raw_conn()
        conn.execute(
            "INSERT INTO event_logs (level, event_type, message, meta) VALUES (?,?,?,?)",
            (level, event_type, message, json.dumps(meta) if meta is not None else None),
        )
        conn.commit()
        conn.close()
    except Exception as e:  # noqa: BLE001 - logging must never crash the app
        logger.error("log_event failed: %s", e)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("admin_logged_in"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "unauthorized"}), 401
            return redirect("/login")
        return f(*args, **kwargs)

    return wrapper


def setup_required(f):
    """Blocks access until the one-time admin account has been created."""

    @wraps(f)
    def wrapper(*args, **kwargs):
        if get_setting("setup_complete") != "1":
            if request.path.startswith("/api/"):
                return jsonify({"error": "setup_required"}), 409
            return redirect("/setup")
        return f(*args, **kwargs)

    return wrapper


# ---------------------------------------------------------------------------
# Meta Graph API helpers — Dual compatible with both Instagram (IG...)
# and Facebook Page (EAA...) tokens.
# ---------------------------------------------------------------------------
def get_base_url(token):
    """Routes to graph.instagram.com for IG tokens, graph.facebook.com for Page tokens."""
    if token and token.strip().startswith("IG"):
        return f"https://graph.instagram.com/{GRAPH_API_VERSION}"
    return f"https://graph.facebook.com/{GRAPH_API_VERSION}"


def graph_request(method, path, params=None, json_body=None, retries=3, base_timeout=8):
    """Resilient wrapper around requests.* for both IG and FB Graph APIs."""
    params = dict(params or {})
    token = params.get("access_token") or (
        json_body.get("access_token") if isinstance(json_body, dict) else ""
    )
    base = get_base_url(token)
    url = f"{base}/{path.lstrip('/')}"

    backoff = 1.0
    last_err = "unknown_error"
    last_status = 0

    for attempt in range(1, retries + 1):
        try:
            resp = requests.request(
                method, url, params=params, json=json_body, timeout=base_timeout
            )
            last_status = resp.status_code
            if resp.status_code == 429 or resp.status_code >= 500:
                last_err = f"HTTP {resp.status_code}: {resp.text[:200]}"
                raise requests.exceptions.RequestException(last_err)
            try:
                return resp.json(), resp.status_code
            except ValueError:
                return {"error": {"message": "Invalid JSON in response"}}, resp.status_code
        except requests.exceptions.RequestException as e:
            last_err = str(e)
            if attempt < retries:
                time.sleep(backoff)
                backoff *= 2
                continue
        except Exception as e:
            last_err = str(e)
            break

    log_event("ERROR", "graph_api_failed", f"{method} {path}: {last_err}")
    return {"error": {"message": last_err}}, last_status


def fetch_instagram_account(access_token):
    """Resolves the IG account whether using an Instagram User token (IG...)
    or a Facebook Page token (EAA...).
    """
    token = (access_token or "").strip()
    if not token:
        return None, "Access token is empty"

    # 1. Handle Instagram-native Token (IG... / IGAA...)
    if token.startswith("IG"):
        data, status = graph_request(
            "GET",
            "me",
            {
                "fields": "user_id,username,profile_picture_url,id",
                "access_token": token,
            },
        )
        if status == 200 and ("id" in data or "user_id" in data):
            return {
                "page_id": "",
                "page_name": "Instagram Account",
                "page_access_token": token,
                "ig_user_id": data.get("user_id") or data.get("id"),
                "ig_username": data.get("username", ""),
                "ig_profile_pic": data.get("profile_picture_url", ""),
            }, None
        msg = data.get("error", {}).get(
            "message", "Failed to validate Instagram token"
        )
        return None, msg

    # 2. Handle Facebook Page Token (EAA...)
    data, status = graph_request(
        "GET",
        "me/accounts",
        {
            "fields": "id,name,access_token,instagram_business_account{id,username,profile_picture_url}",
            "access_token": token,
        },
    )
    if status != 200 or "error" in data:
        msg = data.get("error", {}).get("message", "Failed to fetch connected Pages")
        return None, msg

    for page in data.get("data", []):
        ig = page.get("instagram_business_account")
        if ig:
            return {
                "page_id": page.get("id"),
                "page_name": page.get("name"),
                "page_access_token": page.get("access_token", token),
                "ig_user_id": ig.get("id"),
                "ig_username": ig.get("username", ""),
                "ig_profile_pic": ig.get("profile_picture_url", ""),
            }, None

    return None, "No Instagram Business/Creator account is connected to any of your Pages."


def fetch_recent_media(ig_user_id, access_token, limit=25):
    path = "me/media" if access_token.strip().startswith("IG") else f"{ig_user_id}/media"
    data, status = graph_request(
        "GET",
        path,
        {
            "fields": "id,caption,media_type,media_url,thumbnail_url,permalink,timestamp",
            "limit": limit,
            "access_token": access_token,
        },
    )
    if status != 200 or "error" in data:
        return [], data.get("error", {}).get("message", "Failed to fetch media")
    return data.get("data", []), None


def check_is_follower(igsid, access_token):
    """Checks follower status.

    Note: is_user_follow_business is only supported on Page tokens. Defaults to
    False on IG user tokens so non-follower flow works cleanly.
    """
    if access_token.strip().startswith("IG"):
        return False
    data, status = graph_request(
        "GET",
        igsid,
        {"fields": "is_user_follow_business", "access_token": access_token},
    )
    if status == 200 and isinstance(data, dict) and "is_user_follow_business" in data:
        return bool(data["is_user_follow_business"])
    return False


def send_dm(ig_user_id, recipient_id, message_text, access_token):
    path = "me/messages" if access_token.strip().startswith("IG") else f"{ig_user_id}/messages"
    body = {
        "recipient": {"id": recipient_id},
        "message": {"text": message_text},
    }
    data, status = graph_request(
        "POST", path, {"access_token": access_token}, json_body=body
    )
    if status == 200 and "error" not in data:
        return True, None
    return False, data.get("error", {}).get("message", f"HTTP {status}")


def post_public_reply(comment_id, text, access_token):
    data, status = graph_request(
        "POST",
        f"{comment_id}/replies",
        {"access_token": access_token, "message": text},
    )
    if status == 200 and "error" not in data:
        return True, None
    return False, data.get("error", {}).get("message", f"HTTP {status}")


# ---------------------------------------------------------------------------
# Webhook processing (runs OFF the request thread)
# ---------------------------------------------------------------------------
def match_automation(conn, media_id, text):
    """Media-specific automations win over 'apply to all' ones."""
    rows = conn.execute(
        "SELECT * FROM automations WHERE active=1 "
        "ORDER BY (target_media != 'ALL') DESC, id ASC"
    ).fetchall()
    text_lower = (text or "").lower()
    for row in rows:
        if row["target_media"] != "ALL" and row["target_media"] != media_id:
            continue
        if row["trigger_type"] == "ALL":
            return row
        keywords = [k.strip().lower() for k in (row["keywords"] or "").split(",") if k.strip()]
        if any(kw in text_lower for kw in keywords):
            return row
    return None


def update_processed(conn, comment_id, status, error_message, automation_id, is_follower=None):
    conn.execute(
        "UPDATE processed_comments SET dm_status=?, error_message=?, automation_id=?, "
        "is_follower=COALESCE(?, is_follower) WHERE comment_id=?",
        (status, error_message, automation_id, is_follower, comment_id),
    )
    conn.commit()


def handle_comment_event(value):
    comment_id = value.get("id")
    text = value.get("text", "") or ""
    from_obj = value.get("from") or {}
    commenter_id = from_obj.get("id")
    commenter_username = from_obj.get("username", "")
    media_obj = value.get("media") or {}
    media_id = media_obj.get("id")

    if not comment_id or not commenter_id:
        log_event("WARN", "invalid_payload", "Webhook comment missing id/from.id", value)
        return

    conn = get_raw_conn()
    try:
        cur = conn.execute(
            "INSERT OR IGNORE INTO processed_comments "
            "(comment_id, media_id, commenter_id, commenter_username, comment_text, dm_status) "
            "VALUES (?,?,?,?,?, 'processing')",
            (comment_id, media_id, commenter_id, commenter_username, text),
        )
        conn.commit()
        if cur.rowcount == 0:
            log_event("INFO", "duplicate", f"Duplicate comment ignored: {comment_id}")
            conn.close()
            return
    except sqlite3.Error as e:
        log_event("ERROR", "db_error", f"Insert failed for {comment_id}: {e}")
        conn.close()
        return

    settings = get_all_settings(conn)
    access_token = settings.get("access_token", "")
    ig_user_id = settings.get("ig_user_id", "")

    automation = match_automation(conn, media_id, text)
    if not automation:
        update_processed(conn, comment_id, "skipped", "No matching automation", None)
        conn.close()
        return

    if not access_token or not ig_user_id:
        update_processed(
            conn, comment_id, "failed", "Instagram account not connected", automation["id"]
        )
        conn.close()
        log_event("ERROR", "not_connected", "Comment matched but no IG account is connected")
        return

    is_follower = check_is_follower(commenter_id, access_token)
    message = automation["message_follower"] if is_follower else automation["message_nonfollower"]

    ok, err = send_dm(ig_user_id, commenter_id, message, access_token)

    if automation["public_reply_enabled"] and automation["public_reply_text"]:
        pr_ok, pr_err = post_public_reply(comment_id, automation["public_reply_text"], access_token)
        if not pr_ok:
            log_event("WARN", "public_reply_failed", f"{comment_id}: {pr_err}")

    update_processed(
        conn,
        comment_id,
        "sent" if ok else "failed",
        None if ok else err,
        automation["id"],
        is_follower=1 if is_follower else 0,
    )
    conn.close()
    log_event(
        "INFO" if ok else "ERROR",
        "dm_dispatch",
        f"DM {'sent' if ok else 'failed'} to {commenter_username or commenter_id}",
        {"comment_id": comment_id, "automation": automation["name"]},
    )


def process_webhook_payload(payload):
    with app.app_context():
        try:
            for entry in payload.get("entry", []):
                for change in entry.get("changes", []):
                    if change.get("field") != "comments":
                        continue
                    handle_comment_event(change.get("value") or {})
        except Exception as e:  # noqa: BLE001 - background thread must never die silently
            log_event("ERROR", "webhook_processing", f"Unhandled exception: {e}")
            logger.exception("Webhook processing failed")


# ---------------------------------------------------------------------------
# Webhook routes — MUST return fast. No blocking work happens here.
# ---------------------------------------------------------------------------
@app.route("/webhook", methods=["GET"])
def webhook_verify():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge", "")
    expected = get_setting("verify_token")
    if mode == "subscribe" and token and secrets.compare_digest(token, expected):
        return challenge, 200
    return "Forbidden", 403


@app.route("/webhook", methods=["POST"])
def webhook_receive():
    payload = request.get_json(force=True, silent=True)
    if not payload:
        return jsonify({"status": "ignored"}), 200

    try:
        set_setting("last_webhook_at", str(int(time.time())))
    except Exception:  # noqa: BLE001
        pass

    threading.Thread(target=process_webhook_payload, args=(payload,), daemon=True).start()
    return jsonify({"status": "received"}), 200


# ---------------------------------------------------------------------------
# OAuth handshake (optional path — manual token entry works too)
# ---------------------------------------------------------------------------
@app.route("/oauth/start")
@login_required
def oauth_start():
    app_id = get_setting("ig_app_id")
    if not app_id:
        return redirect("/?tab=settings&oauth=missing_app_id")
    redirect_uri = request.host_url.rstrip("/") + "/oauth/callback"
    scopes = "pages_show_list,instagram_basic,instagram_manage_comments,instagram_manage_messages,pages_read_engagement"
    auth_url = (
        f"https://www.facebook.com/{GRAPH_API_VERSION}/dialog/oauth"
        f"?client_id={app_id}&redirect_uri={redirect_uri}&scope={scopes}&response_type=code"
    )
    return redirect(auth_url)


@app.route("/oauth/callback")
@login_required
def oauth_callback():
    code = request.args.get("code")
    if not code:
        return redirect("/?tab=settings&oauth=denied")

    app_id = get_setting("ig_app_id")
    app_secret = get_setting("ig_app_secret")
    redirect_uri = request.host_url.rstrip("/") + "/oauth/callback"

    short_data, status = graph_request(
        "GET",
        "oauth/access_token",
        {
            "client_id": app_id,
            "redirect_uri": redirect_uri,
            "client_secret": app_secret,
            "code": code,
        },
    )
    short_token = short_data.get("access_token") if status == 200 else None
    if not short_token:
        return redirect("/?tab=settings&oauth=token_exchange_failed")

    long_data, status = graph_request(
        "GET",
        "oauth/access_token",
        {
            "grant_type": "fb_exchange_token",
            "client_id": app_id,
            "client_secret": app_secret,
            "fb_exchange_token": short_token,
        },
    )
    long_token = long_data.get("access_token") if status == 200 else short_token

    account, err = fetch_instagram_account(long_token)
    if err:
        return redirect(f"/?tab=settings&oauth=connect_failed")

    set_setting("access_token", account["page_access_token"])
    set_setting("page_id", account["page_id"])
    set_setting("ig_user_id", account["ig_user_id"])
    set_setting("ig_username", account["ig_username"])
    set_setting("ig_profile_pic", account["ig_profile_pic"])
    set_setting("last_token_valid", "1")
    set_setting("last_token_check", str(int(time.time())))
    return redirect("/?tab=settings&oauth=success")


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------
@app.route("/setup", methods=["GET"])
def setup_page():
    if get_setting("setup_complete") == "1":
        return redirect("/login")
    return Response(SETUP_HTML, mimetype="text/html")


@app.route("/api/setup", methods=["POST"])
def api_setup():
    if get_setting("setup_complete") == "1":
        return jsonify({"error": "Setup already completed"}), 409
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    if len(username) < 3:
        return jsonify({"error": "Username must be at least 3 characters"}), 400
    if len(password) < 8:
        return jsonify({"error": "Password must be at least 8 characters"}), 400

    set_setting("admin_username", username)
    set_setting("admin_password_hash", generate_password_hash(password))
    set_setting("setup_complete", "1")

    session.permanent = True
    session["admin_logged_in"] = True
    session["admin_username"] = username
    return jsonify({"status": "ok"})


@app.route("/login", methods=["GET"])
@setup_required
def login_page():
    if session.get("admin_logged_in"):
        return redirect("/")
    return Response(LOGIN_HTML, mimetype="text/html")


@app.route("/api/login", methods=["POST"])
@setup_required
def api_login():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""

    stored_user = get_setting("admin_username")
    stored_hash = get_setting("admin_password_hash")

    if username and stored_user and secrets.compare_digest(username, stored_user) and \
            stored_hash and check_password_hash(stored_hash, password):
        session.permanent = True
        session["admin_logged_in"] = True
        session["admin_username"] = username
        return jsonify({"status": "ok"})

    return jsonify({"error": "Invalid username or password"}), 401


@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.clear()
    return jsonify({"status": "ok"})


@app.route("/api/session", methods=["GET"])
def api_session():
    return jsonify(
        {
            "setup_complete": get_setting("setup_complete") == "1",
            "logged_in": bool(session.get("admin_logged_in")),
            "username": session.get("admin_username", ""),
        }
    )


# ---------------------------------------------------------------------------
# Dashboard shell
# ---------------------------------------------------------------------------
@app.route("/")
@setup_required
@login_required
def index():
    return Response(DASHBOARD_HTML, mimetype="text/html")


# ---------------------------------------------------------------------------
# Settings API
# ---------------------------------------------------------------------------
def _mask(value, keep=4):
    if not value:
        return ""
    if len(value) <= keep:
        return "•" * len(value)
    return "•" * (len(value) - keep) + value[-keep:]


@app.route("/api/settings", methods=["GET"])
@login_required
def api_get_settings():
    s = get_all_settings()
    return jsonify(
        {
            "ig_app_id": s.get("ig_app_id", ""),
            "ig_app_secret_set": bool(s.get("ig_app_secret")),
            "ig_app_secret_masked": _mask(s.get("ig_app_secret", "")),
            "access_token_set": bool(s.get("access_token")),
            "access_token_masked": _mask(s.get("access_token", "")),
            "verify_token": s.get("verify_token", ""),
            "ig_user_id": s.get("ig_user_id", ""),
            "ig_username": s.get("ig_username", ""),
            "ig_profile_pic": s.get("ig_profile_pic", ""),
            "page_id": s.get("page_id", ""),
            "connected": bool(s.get("ig_user_id")),
            "webhook_url": request.host_url.rstrip("/") + "/webhook",
            "last_webhook_at": s.get("last_webhook_at", ""),
            "last_token_valid": s.get("last_token_valid", "0") == "1",
            "last_token_check": s.get("last_token_check", ""),
        }
    )


@app.route("/api/settings", methods=["POST"])
@login_required
def api_save_settings():
    data = request.get_json(silent=True) or {}

    if "ig_app_id" in data:
        set_setting("ig_app_id", (data.get("ig_app_id") or "").strip())
    if data.get("ig_app_secret"):
        set_setting("ig_app_secret", data["ig_app_secret"].strip())
    if data.get("regenerate_verify_token"):
        set_setting("verify_token", secrets.token_hex(16))

    result = {"status": "ok", "connected": False}

    access_token = data.get("access_token", "").strip() if data.get("access_token") else ""
    if access_token:
        set_setting("access_token", access_token)
        account, err = fetch_instagram_account(access_token)
        if err:
            set_setting("last_token_valid", "0")
            set_setting("last_token_check", str(int(time.time())))
            result["connect_error"] = err
        else:
            set_setting("access_token", account["page_access_token"])
            set_setting("page_id", account["page_id"])
            set_setting("ig_user_id", account["ig_user_id"])
            set_setting("ig_username", account["ig_username"])
            set_setting("ig_profile_pic", account["ig_profile_pic"])
            set_setting("last_token_valid", "1")
            set_setting("last_token_check", str(int(time.time())))
            result["connected"] = True
            result["ig_username"] = account["ig_username"]

    return jsonify(result)


@app.route("/api/settings/test", methods=["POST"])
@login_required
def api_test_connection():
    token = get_setting("access_token")
    if not token:
        return jsonify({"valid": False, "error": "No access token saved yet"}), 400
    account, err = fetch_instagram_account(token)
    valid = err is None
    set_setting("last_token_valid", "1" if valid else "0")
    set_setting("last_token_check", str(int(time.time())))
    if valid:
        set_setting("ig_user_id", account["ig_user_id"])
        set_setting("ig_username", account["ig_username"])
        set_setting("ig_profile_pic", account["ig_profile_pic"])
        set_setting("page_id", account["page_id"])
    return jsonify({"valid": valid, "error": err, "ig_username": account["ig_username"] if valid else None})


@app.route("/api/media", methods=["GET"])
@login_required
def api_media():
    token = get_setting("access_token")
    ig_user_id = get_setting("ig_user_id")
    if not token or not ig_user_id:
        return jsonify({"error": "Connect your Instagram account first", "media": []}), 400
    media, err = fetch_recent_media(ig_user_id, token)
    if err:
        return jsonify({"error": err, "media": []}), 502
    return jsonify({"media": media})


# ---------------------------------------------------------------------------
# Automations API
# ---------------------------------------------------------------------------
def _row_to_automation(row):
    return {
        "id": row["id"],
        "name": row["name"],
        "target_media": row["target_media"],
        "target_media_caption": row["target_media_caption"],
        "target_media_thumb": row["target_media_thumb"],
        "trigger_type": row["trigger_type"],
        "keywords": row["keywords"],
        "message_follower": row["message_follower"],
        "message_nonfollower": row["message_nonfollower"],
        "public_reply_enabled": bool(row["public_reply_enabled"]),
        "public_reply_text": row["public_reply_text"],
        "active": bool(row["active"]),
        "created_at": row["created_at"],
    }


@app.route("/api/automations", methods=["GET"])
@login_required
def api_list_automations():
    rows = get_db().execute("SELECT * FROM automations ORDER BY id DESC").fetchall()
    return jsonify({"automations": [_row_to_automation(r) for r in rows]})


def _validate_automation(data):
    if not (data.get("name") or "").strip():
        return "Name is required"
    if not (data.get("message_follower") or "").strip():
        return "Message A (followers) is required"
    if not (data.get("message_nonfollower") or "").strip():
        return "Message B (non-followers) is required"
    if data.get("trigger_type") == "KEYWORDS" and not (data.get("keywords") or "").strip():
        return "Add at least one keyword, or switch to 'Reply to every comment'"
    if len(data.get("message_follower", "")) > 1000 or len(data.get("message_nonfollower", "")) > 1000:
        return "Messages must be under 1000 characters"
    return None


@app.route("/api/automations", methods=["POST"])
@login_required
def api_create_automation():
    data = request.get_json(silent=True) or {}
    err = _validate_automation(data)
    if err:
        return jsonify({"error": err}), 400

    db = get_db()
    cur = db.execute(
        "INSERT INTO automations (name, target_media, target_media_caption, target_media_thumb, "
        "trigger_type, keywords, message_follower, message_nonfollower, public_reply_enabled, "
        "public_reply_text, active) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            data.get("name", "").strip(),
            data.get("target_media", "ALL") or "ALL",
            data.get("target_media_caption", "")[:200],
            data.get("target_media_thumb", ""),
            data.get("trigger_type", "ALL"),
            data.get("keywords", "").strip(),
            data.get("message_follower", "").strip(),
            data.get("message_nonfollower", "").strip(),
            1 if data.get("public_reply_enabled") else 0,
            data.get("public_reply_text", "").strip(),
            1 if data.get("active", True) else 0,
        ),
    )
    db.commit()
    row = db.execute("SELECT * FROM automations WHERE id=?", (cur.lastrowid,)).fetchone()
    return jsonify({"automation": _row_to_automation(row)}), 201


@app.route("/api/automations/<int:automation_id>", methods=["PUT"])
@login_required
def api_update_automation(automation_id):
    data = request.get_json(silent=True) or {}
    err = _validate_automation(data)
    if err:
        return jsonify({"error": err}), 400

    db = get_db()
    existing = db.execute("SELECT id FROM automations WHERE id=?", (automation_id,)).fetchone()
    if not existing:
        return jsonify({"error": "Automation not found"}), 404

    db.execute(
        "UPDATE automations SET name=?, target_media=?, target_media_caption=?, target_media_thumb=?, "
        "trigger_type=?, keywords=?, message_follower=?, message_nonfollower=?, public_reply_enabled=?, "
        "public_reply_text=?, active=? WHERE id=?",
        (
            data.get("name", "").strip(),
            data.get("target_media", "ALL") or "ALL",
            data.get("target_media_caption", "")[:200],
            data.get("target_media_thumb", ""),
            data.get("trigger_type", "ALL"),
            data.get("keywords", "").strip(),
            data.get("message_follower", "").strip(),
            data.get("message_nonfollower", "").strip(),
            1 if data.get("public_reply_enabled") else 0,
            data.get("public_reply_text", "").strip(),
            1 if data.get("active", True) else 0,
            automation_id,
        ),
    )
    db.commit()
    row = db.execute("SELECT * FROM automations WHERE id=?", (automation_id,)).fetchone()
    return jsonify({"automation": _row_to_automation(row)})


@app.route("/api/automations/<int:automation_id>", methods=["DELETE"])
@login_required
def api_delete_automation(automation_id):
    db = get_db()
    db.execute("DELETE FROM automations WHERE id=?", (automation_id,))
    db.commit()
    return jsonify({"status": "ok"})


@app.route("/api/automations/<int:automation_id>/toggle", methods=["POST"])
@login_required
def api_toggle_automation(automation_id):
    db = get_db()
    row = db.execute("SELECT active FROM automations WHERE id=?", (automation_id,)).fetchone()
    if not row:
        return jsonify({"error": "Automation not found"}), 404
    new_state = 0 if row["active"] else 1
    db.execute("UPDATE automations SET active=? WHERE id=?", (new_state, automation_id))
    db.commit()
    return jsonify({"active": bool(new_state)})


# ---------------------------------------------------------------------------
# Metrics & logs API
# ---------------------------------------------------------------------------
@app.route("/api/metrics", methods=["GET"])
@login_required
def api_metrics():
    db = get_db()

    def scalar(sql, params=()):
        row = db.execute(sql, params).fetchone()
        return list(row)[0] if row else 0

    duplicates = scalar("SELECT COUNT(*) FROM event_logs WHERE event_type='duplicate'")
    stored_comments = scalar("SELECT COUNT(*) FROM processed_comments")

    return jsonify(
        {
            "comments_received": stored_comments + duplicates,
            "dms_delivered": scalar("SELECT COUNT(*) FROM processed_comments WHERE dm_status='sent'"),
            "follower_dms": scalar(
                "SELECT COUNT(*) FROM processed_comments WHERE dm_status='sent' AND is_follower=1"
            ),
            "nonfollower_dms": scalar(
                "SELECT COUNT(*) FROM processed_comments WHERE dm_status='sent' AND is_follower=0"
            ),
            "failed": scalar("SELECT COUNT(*) FROM processed_comments WHERE dm_status='failed'"),
            "duplicates": duplicates,
            "active_automations": scalar("SELECT COUNT(*) FROM automations WHERE active=1"),
        }
    )


@app.route("/api/logs", methods=["GET"])
@login_required
def api_logs():
    status = request.args.get("status", "all")
    limit = min(int(request.args.get("limit", 50) or 50), 200)

    db = get_db()
    sql = (
        "SELECT pc.*, a.name AS automation_name FROM processed_comments pc "
        "LEFT JOIN automations a ON a.id = pc.automation_id "
    )
    params = []
    if status != "all":
        sql += "WHERE pc.dm_status = ? "
        params.append(status)
    sql += "ORDER BY pc.created_at DESC LIMIT ?"
    params.append(limit)

    rows = db.execute(sql, params).fetchall()
    logs = [
        {
            "comment_id": r["comment_id"],
            "commenter_username": r["commenter_username"] or r["commenter_id"],
            "comment_text": r["comment_text"],
            "is_follower": bool(r["is_follower"]) if r["is_follower"] is not None else None,
            "dm_status": r["dm_status"],
            "error_message": r["error_message"],
            "automation_name": r["automation_name"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]
    return jsonify({"logs": logs})


@app.route("/api/status", methods=["GET"])
@login_required
def api_status():
    s = get_all_settings()
    last_webhook_at = s.get("last_webhook_at", "")
    return jsonify(
        {
            "webhook_active": True,
            "webhook_url": request.host_url.rstrip("/") + "/webhook",
            "last_webhook_at": last_webhook_at,
            "token_valid": s.get("last_token_valid", "0") == "1",
            "last_token_check": s.get("last_token_check", ""),
            "ig_connected": bool(s.get("ig_user_id")),
            "ig_username": s.get("ig_username", ""),
        }
    )


# ---------------------------------------------------------------------------
# Embedded frontend — Tailwind CDN, vanilla JS, no build step.
# Design language mirrors a soft-neutral, pill-input aesthetic:
# rounded-full fields, chip tags, black primary actions, mono data.
# ---------------------------------------------------------------------------
BASE_STYLE = """
<style>
  :root {
    --bg: #f7f7f6;
    --surface: #ffffff;
    --field: #eeeeec;
    --field-border: #e3e3e0;
    --border: #e6e6e3;
    --text: #131311;
    --text-soft: #6d6d68;
    --text-faint: #9c9c96;
    --accent: #0d0d0c;
    --success: #15803d;
    --success-bg: #eefbf1;
    --danger: #b91c1c;
    --danger-bg: #fdeeee;
    --warn: #b45309;
    --warn-bg: #fdf3e7;
  }
  * { -webkit-font-smoothing: antialiased; }
  body { background: var(--bg); color: var(--text); font-family: 'Inter', ui-sans-serif, system-ui, sans-serif; }
  .font-mono { font-family: 'JetBrains Mono', ui-monospace, monospace; }
  .field {
    background: var(--field); border: 1px solid var(--field-border); border-radius: 9999px;
    padding: 0.65rem 1.1rem; font-size: 0.9rem; color: var(--text); width: 100%;
    transition: border-color .15s ease, box-shadow .15s ease;
  }
  .field:focus { outline: none; border-color: #c9c9c3; box-shadow: 0 0 0 3px rgba(0,0,0,0.04); }
  textarea.field { border-radius: 1.25rem; resize: vertical; }
  .field-flat { background: var(--field); border: 1px solid var(--field-border); border-radius: 1rem; padding: 0.65rem 1.1rem; }
  .chip {
    display:inline-flex; align-items:center; gap:0.4rem; background:#fff; border:1px solid var(--border);
    border-radius:9999px; padding:0.3rem 0.55rem 0.3rem 0.35rem; font-size:0.82rem;
  }
  .chip-x { cursor:pointer; color:var(--text-faint); }
  .chip-x:hover { color:var(--text); }
  .btn-primary {
    background: var(--accent); color:#fff; border-radius:9999px; padding:0.65rem 1.4rem;
    font-size:0.88rem; font-weight:600; transition: opacity .15s ease; border:1px solid var(--accent);
  }
  .btn-primary:hover { opacity:0.85; }
  .btn-primary:disabled { opacity:0.4; cursor:not-allowed; }
  .btn-secondary {
    background:#fff; color:var(--text); border:1px solid var(--border); border-radius:9999px;
    padding:0.65rem 1.4rem; font-size:0.88rem; font-weight:600; transition: background .15s ease;
  }
  .btn-secondary:hover { background:#f4f4f2; }
  .btn-ghost { color:var(--text-soft); font-size:0.85rem; font-weight:500; }
  .btn-ghost:hover { color:var(--text); }
  .card { background: var(--surface); border:1px solid var(--border); border-radius:1.5rem; }
  .segmented { display:inline-flex; background: var(--field); border-radius:9999px; padding:0.25rem; gap:0.15rem; }
  .segmented button { padding:0.45rem 1rem; border-radius:9999px; font-size:0.85rem; font-weight:500; color:var(--text-soft); }
  .segmented button.active { background:#fff; color:var(--text); box-shadow: 0 1px 2px rgba(0,0,0,0.06); }
  .dot { width:7px; height:7px; border-radius:9999px; display:inline-block; }
  .badge { font-size:0.72rem; font-weight:600; padding:0.2rem 0.55rem; border-radius:9999px; letter-spacing:0.02em; }
  .badge-sent { background: var(--success-bg); color: var(--success); }
  .badge-failed { background: var(--danger-bg); color: var(--danger); }
  .badge-skipped { background: var(--field); color: var(--text-soft); }
  .badge-processing { background: var(--warn-bg); color: var(--warn); }
  .label { font-size:0.82rem; font-weight:600; color:var(--text); }
  .hint { font-size:0.78rem; color: var(--text-faint); }
  ::placeholder { color: var(--text-faint); }
  .scrollbar-thin::-webkit-scrollbar { height:6px; width:6px; }
  .scrollbar-thin::-webkit-scrollbar-thumb { background: var(--field-border); border-radius:9999px; }
  .modal-backdrop { background: rgba(20,20,18,0.35); backdrop-filter: blur(2px); }
  .step-dot { width:1.6rem; height:1.6rem; border-radius:9999px; display:flex; align-items:center; justify-content:center; font-size:0.75rem; font-weight:700; }
  .step-dot.done { background: var(--accent); color:#fff; }
  .step-dot.current { background:#fff; border:2px solid var(--accent); color:var(--accent); }
  .step-dot.todo { background: var(--field); color: var(--text-faint); }
  .media-tile { border-radius:1rem; overflow:hidden; border:2px solid transparent; cursor:pointer; position:relative; aspect-ratio:1/1; background:var(--field); }
  .media-tile.selected { border-color: var(--accent); }
  .media-tile img { width:100%; height:100%; object-fit:cover; }
  .toast { animation: toast-in .2s ease; }
  @keyframes toast-in { from { opacity:0; transform: translateY(6px);} to { opacity:1; transform:translateY(0);} }
  input[type=checkbox].switch { appearance:none; width:2.4rem; height:1.4rem; background:var(--field-border); border-radius:9999px; position:relative; cursor:pointer; transition:background .15s ease; }
  input[type=checkbox].switch:checked { background: var(--accent); }
  input[type=checkbox].switch::after { content:''; position:absolute; top:2px; left:2px; width:1.1rem; height:1.1rem; background:#fff; border-radius:9999px; transition: left .15s ease; box-shadow:0 1px 2px rgba(0,0,0,0.2); }
  input[type=checkbox].switch:checked::after { left:1.1rem; }
</style>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600;700&display=swap" rel="stylesheet">
<script src="https://cdn.tailwindcss.com"></script>
"""

# ---------------------------------------------------------------------------
# Auth pages
# ---------------------------------------------------------------------------
LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Sign in · CommentDM</title>
""" + BASE_STYLE + """
</head>
<body class="min-h-screen flex items-center justify-center px-4">
  <div class="w-full max-w-sm">
    <div class="text-center mb-8">
      <div class="w-11 h-11 rounded-2xl bg-[var(--accent)] mx-auto mb-4 flex items-center justify-center text-white font-bold">C</div>
      <h1 class="text-xl font-bold">Sign in</h1>
      <p class="hint mt-1">Access your CommentDM dashboard</p>
    </div>
    <div class="card p-6 space-y-4">
      <div>
        <label class="label block mb-1.5">Username</label>
        <input id="username" class="field" placeholder="admin" autocomplete="username">
      </div>
      <div>
        <label class="label block mb-1.5">Password</label>
        <input id="password" type="password" class="field" placeholder="Your password" autocomplete="current-password">
      </div>
      <div id="error" class="hidden text-sm text-[var(--danger)] font-medium"></div>
      <button id="submit" class="btn-primary w-full">Sign in</button>
    </div>
  </div>
<script>
const $ = id => document.getElementById(id);
async function submit() {
  const btn = $('submit'); btn.disabled = true; btn.textContent = 'Signing in…';
  $('error').classList.add('hidden');
  try {
    const res = await fetch('/api/login', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({username: $('username').value, password: $('password').value})
    });
    const data = await res.json();
    if (res.ok) { window.location.href = '/'; return; }
    $('error').textContent = data.error || 'Invalid credentials';
    $('error').classList.remove('hidden');
  } catch (e) {
    $('error').textContent = 'Network error — please try again';
    $('error').classList.remove('hidden');
  }
  btn.disabled = false; btn.textContent = 'Sign in';
}
$('submit').addEventListener('click', submit);
$('password').addEventListener('keydown', e => { if (e.key === 'Enter') submit(); });
</script>
</body>
</html>
"""

SETUP_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Create admin account · CommentDM</title>
""" + BASE_STYLE + """
</head>
<body class="min-h-screen flex items-center justify-center px-4">
  <div class="w-full max-w-sm">
    <div class="text-center mb-8">
      <div class="w-11 h-11 rounded-2xl bg-[var(--accent)] mx-auto mb-4 flex items-center justify-center text-white font-bold">C</div>
      <h1 class="text-xl font-bold">Create your admin account</h1>
      <p class="hint mt-1">One-time setup — this protects your dashboard</p>
    </div>
    <div class="card p-6 space-y-4">
      <div>
        <label class="label block mb-1.5">Username</label>
        <input id="username" class="field" placeholder="admin" autocomplete="username">
      </div>
      <div>
        <label class="label block mb-1.5">Password</label>
        <input id="password" type="password" class="field" placeholder="At least 8 characters" autocomplete="new-password">
        <p class="hint mt-1.5">Must be at least 8 characters.</p>
      </div>
      <div id="error" class="hidden text-sm text-[var(--danger)] font-medium"></div>
      <button id="submit" class="btn-primary w-full">Create account</button>
    </div>
  </div>
<script>
const $ = id => document.getElementById(id);
async function submit() {
  const btn = $('submit'); btn.disabled = true; btn.textContent = 'Creating…';
  $('error').classList.add('hidden');
  try {
    const res = await fetch('/api/setup', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({username: $('username').value, password: $('password').value})
    });
    const data = await res.json();
    if (res.ok) { window.location.href = '/'; return; }
    $('error').textContent = data.error || 'Something went wrong';
    $('error').classList.remove('hidden');
  } catch (e) {
    $('error').textContent = 'Network error — please try again';
    $('error').classList.remove('hidden');
  }
  btn.disabled = false; btn.textContent = 'Create account';
}
$('submit').addEventListener('click', submit);
$('password').addEventListener('keydown', e => { if (e.key === 'Enter') submit(); });
</script>
</body>
</html>
"""

# ---------------------------------------------------------------------------
# Dashboard shell (SPA-lite: tabs + modal wizard, all data via fetch)
# ---------------------------------------------------------------------------
DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>CommentDM · Dashboard</title>
""" + BASE_STYLE + """
</head>
<body class="min-h-screen pb-24">

  <!-- Top bar -->
  <header class="border-b" style="border-color:var(--border); background:var(--surface);">
    <div class="max-w-6xl mx-auto px-5 py-4 flex items-center justify-between">
      <div class="flex items-center gap-2.5">
        <div class="w-8 h-8 rounded-xl bg-[var(--accent)] flex items-center justify-center text-white font-bold text-sm">C</div>
        <span class="font-bold text-[15px]">CommentDM</span>
      </div>
      <div class="flex items-center gap-3">
        <div class="hidden sm:flex items-center gap-1.5 field-flat py-1.5 px-3">
          <span id="dot-webhook" class="dot" style="background:var(--success)"></span>
          <span class="text-xs font-medium text-[var(--text-soft)]">Webhook Active</span>
        </div>
        <div class="hidden sm:flex items-center gap-1.5 field-flat py-1.5 px-3">
          <span id="dot-token" class="dot" style="background:var(--text-faint)"></span>
          <span id="token-label" class="text-xs font-medium text-[var(--text-soft)]">Token Unknown</span>
        </div>
        <button id="logout-btn" class="btn-ghost">Log out</button>
      </div>
    </div>
  </header>

  <main class="max-w-6xl mx-auto px-5 pt-8">

    <!-- Metrics -->
    <div class="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-3 mb-8">
      <div class="card p-4"><p class="hint mb-1.5">Comments received</p><p id="m-received" class="text-2xl font-bold font-mono">0</p></div>
      <div class="card p-4"><p class="hint mb-1.5">DMs delivered</p><p id="m-delivered" class="text-2xl font-bold font-mono" style="color:var(--success)">0</p></div>
      <div class="card p-4"><p class="hint mb-1.5">Follower DMs</p><p id="m-follower" class="text-2xl font-bold font-mono">0</p></div>
      <div class="card p-4"><p class="hint mb-1.5">Non-follower DMs</p><p id="m-nonfollower" class="text-2xl font-bold font-mono">0</p></div>
      <div class="card p-4"><p class="hint mb-1.5">Failed</p><p id="m-failed" class="text-2xl font-bold font-mono" style="color:var(--danger)">0</p></div>
      <div class="card p-4"><p class="hint mb-1.5">Duplicates ignored</p><p id="m-duplicates" class="text-2xl font-bold font-mono">0</p></div>
    </div>

    <!-- Tabs -->
    <div class="flex items-center justify-between mb-5 flex-wrap gap-3">
      <div class="segmented" id="tabs">
        <button data-tab="automations" class="active">Automations</button>
        <button data-tab="logs">Activity Log</button>
        <button data-tab="settings">Settings</button>
      </div>
      <button id="new-automation-btn" class="btn-primary hidden">+ New automation</button>
    </div>

    <!-- Automations tab -->
    <section id="tab-automations" class="space-y-3"></section>

    <!-- Logs tab -->
    <section id="tab-logs" class="hidden">
      <div class="segmented mb-4" id="log-filters">
        <button data-status="all" class="active">All</button>
        <button data-status="sent">Sent</button>
        <button data-status="failed">Failed</button>
        <button data-status="skipped">Skipped</button>
      </div>
      <div class="card overflow-x-auto scrollbar-thin">
        <table class="w-full text-sm">
          <thead>
            <tr class="text-left hint uppercase tracking-wide border-b" style="border-color:var(--border)">
              <th class="px-4 py-3 font-semibold">Time</th>
              <th class="px-4 py-3 font-semibold">Commenter</th>
              <th class="px-4 py-3 font-semibold">Comment</th>
              <th class="px-4 py-3 font-semibold">Automation</th>
              <th class="px-4 py-3 font-semibold">Follower</th>
              <th class="px-4 py-3 font-semibold">Status</th>
            </tr>
          </thead>
          <tbody id="logs-body"></tbody>
        </table>
        <p id="logs-empty" class="hidden text-center hint py-10">No events yet — comments will appear here once your webhook starts receiving traffic.</p>
      </div>
    </section>

    <!-- Settings tab -->
    <section id="tab-settings" class="hidden max-w-2xl space-y-6">
      <div class="card p-6">
        <h3 class="font-bold mb-1">Instagram connection</h3>
        <p class="hint mb-5">Connect the Page behind your Instagram Business or Creator account.</p>

        <div id="connected-preview" class="hidden flex items-center gap-3 mb-5 field-flat py-3 px-4">
          <img id="conn-pic" class="w-9 h-9 rounded-full object-cover bg-[var(--field)]" src="">
          <div>
            <p class="text-sm font-semibold">@<span id="conn-username"></span></p>
            <p class="hint">Connected</p>
          </div>
        </div>

        <div class="space-y-4">
          <div>
            <label class="label block mb-1.5">Meta App ID</label>
            <input id="set-app-id" class="field" placeholder="1234567890">
          </div>
          <div>
            <label class="label block mb-1.5">Meta App Secret</label>
            <input id="set-app-secret" type="password" class="field" placeholder="Leave blank to keep current">
          </div>
          <div class="relative">
            <label class="label block mb-1.5">Long-lived Page Access Token</label>
            <input id="set-token" type="password" class="field pr-10" placeholder="Leave blank to keep current">
            <button id="toggle-token" type="button" class="absolute right-3.5 top-[2.35rem] text-[var(--text-faint)]">👁</button>
            <p class="hint mt-1.5" id="token-hint">Must be at least 8 characters.</p>
          </div>
          <div id="settings-error" class="hidden text-sm text-[var(--danger)] font-medium"></div>
          <div class="flex items-center gap-2 flex-wrap">
            <button id="save-settings-btn" class="btn-primary">Save &amp; connect</button>
            <button id="oauth-btn" class="btn-secondary">Connect via OAuth</button>
            <button id="test-conn-btn" class="btn-secondary">Test connection</button>
          </div>
        </div>
      </div>

      <div class="card p-6">
        <h3 class="font-bold mb-1">Webhook</h3>
        <p class="hint mb-4">Paste these into your Meta App's Instagram webhook configuration.</p>
        <div class="space-y-3">
          <div>
            <label class="label block mb-1.5">Callback URL</label>
            <div class="flex items-center gap-2">
              <input id="webhook-url" class="field font-mono text-xs" readonly>
              <button data-copy="webhook-url" class="btn-secondary shrink-0">Copy</button>
            </div>
          </div>
          <div>
            <label class="label block mb-1.5">Verify token</label>
            <div class="flex items-center gap-2">
              <input id="verify-token" class="field font-mono text-xs" readonly>
              <button data-copy="verify-token" class="btn-secondary shrink-0">Copy</button>
            </div>
          </div>
        </div>
      </div>
    </section>
  </main>

  <!-- ============================= Wizard modal ============================= -->
  <div id="wizard-backdrop" class="hidden fixed inset-0 modal-backdrop z-40 flex items-center justify-center p-4">
    <div class="card w-full max-w-xl max-h-[90vh] overflow-y-auto scrollbar-thin p-6 sm:p-7">
      <div class="flex items-center justify-between mb-6">
        <h2 id="wizard-title" class="font-bold text-lg">New automation</h2>
        <div class="flex items-center gap-1.5">
          <span class="step-dot" data-step-dot="1">1</span>
          <span class="step-dot" data-step-dot="2">2</span>
          <span class="step-dot" data-step-dot="3">3</span>
          <span class="step-dot" data-step-dot="4">4</span>
        </div>
      </div>

      <!-- Step 1 -->
      <div class="wizard-step" data-step="1">
        <label class="label block mb-1.5">Automation name</label>
        <input id="w-name" class="field mb-5" placeholder="e.g. Free guide giveaway">

        <div class="flex items-center justify-between mb-3">
          <label class="label">Select media</label>
          <label class="flex items-center gap-2 text-sm cursor-pointer">
            <input type="checkbox" id="w-apply-all" class="switch">
            Apply to all posts
          </label>
        </div>
        <div id="media-grid" class="grid grid-cols-3 sm:grid-cols-4 gap-2.5 mb-2 max-h-64 overflow-y-auto scrollbar-thin"></div>
        <p id="media-status" class="hint"></p>
      </div>

      <!-- Step 2 -->
      <div class="wizard-step hidden" data-step="2">
        <label class="label block mb-3">Trigger rule</label>
        <div class="space-y-2.5 mb-5">
          <label class="flex items-start gap-3 field-flat p-4 cursor-pointer">
            <input type="radio" name="w-trigger" value="ALL" class="mt-0.5" checked>
            <span>
              <span class="block font-semibold text-sm">Reply to every comment</span>
              <span class="hint">Every comment on the selected media triggers this automation.</span>
            </span>
          </label>
          <label class="flex items-start gap-3 field-flat p-4 cursor-pointer">
            <input type="radio" name="w-trigger" value="KEYWORDS" class="mt-0.5">
            <span>
              <span class="block font-semibold text-sm">Match specific keywords</span>
              <span class="hint">Only comments containing one of these words trigger it (case-insensitive).</span>
            </span>
          </label>
        </div>
        <div id="keywords-wrap" class="hidden">
          <label class="label block mb-1.5">Keywords</label>
          <div id="keyword-chips" class="field flex flex-wrap items-center gap-1.5" style="border-radius:1.25rem; min-height:2.9rem;">
            <input id="keyword-input" class="flex-1 min-w-[100px] bg-transparent outline-none text-sm" placeholder="Type a keyword, press Enter…">
          </div>
          <p class="hint mt-1.5">Comma-separated, case-insensitive.</p>
        </div>
      </div>

      <!-- Step 3 -->
      <div class="wizard-step hidden" data-step="3">
        <div class="mb-5">
          <label class="label block mb-1.5">Message A — followers</label>
          <textarea id="w-msg-follower" class="field" rows="3" placeholder="Thanks for following! Here's your link: …"></textarea>
        </div>
        <div>
          <label class="label block mb-1.5">Message B — non-followers</label>
          <textarea id="w-msg-nonfollower" class="field" rows="3" placeholder="Follow the account to unlock this link 👀"></textarea>
        </div>
        <p class="hint mt-2">We check <span class="font-mono">is_user_follow_business</span> to route the right message automatically.</p>
      </div>

      <!-- Step 4 -->
      <div class="wizard-step hidden" data-step="4">
        <label class="flex items-center justify-between field-flat p-4 cursor-pointer mb-4">
          <span>
            <span class="block font-semibold text-sm">Post a public reply</span>
            <span class="hint">Optional — reply on the comment thread too.</span>
          </span>
          <input type="checkbox" id="w-public-reply-enabled" class="switch">
        </label>
        <div id="public-reply-wrap" class="hidden">
          <label class="label block mb-1.5">Public reply text</label>
          <input id="w-public-reply-text" class="field" placeholder="Check your DMs! 🚀">
        </div>
      </div>

      <div id="wizard-error" class="hidden text-sm text-[var(--danger)] font-medium mt-5"></div>

      <div class="flex items-center justify-between mt-7">
        <button id="w-back" class="btn-secondary">Back</button>
        <div class="flex items-center gap-2">
          <button id="w-cancel" class="btn-secondary">Cancel</button>
          <button id="w-next" class="btn-primary">Next</button>
        </div>
      </div>
    </div>
  </div>

  <div id="toast" class="hidden fixed bottom-6 left-1/2 -translate-x-1/2 z-50 toast">
    <div class="bg-[var(--accent)] text-white text-sm font-medium px-4 py-2.5 rounded-full shadow-lg" id="toast-text"></div>
  </div>

<script>
const $ = id => document.getElementById(id);
const esc = s => (s ?? '').toString().replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

function toast(msg, isError) {
  const t = $('toast'), tx = $('toast-text');
  tx.textContent = msg;
  tx.style.background = '';
  t.querySelector('div').style.background = isError ? 'var(--danger)' : 'var(--accent)';
  t.classList.remove('hidden');
  clearTimeout(window.__toastTimer);
  window.__toastTimer = setTimeout(() => t.classList.add('hidden'), 2600);
}

async function api(path, opts) {
  const res = await fetch(path, Object.assign({headers: {'Content-Type': 'application/json'}}, opts || {}));
  if (res.status === 401) { window.location.href = '/login'; throw new Error('unauthorized'); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw Object.assign(new Error(data.error || 'Request failed'), {data});
  return data;
}

/* ---------------- Tabs ---------------- */
const tabs = ['automations', 'logs', 'settings'];
function showTab(name) {
  tabs.forEach(t => {
    $('tab-' + t).classList.toggle('hidden', t !== name);
  });
  document.querySelectorAll('#tabs button').forEach(b => b.classList.toggle('active', b.dataset.tab === name));
  $('new-automation-btn').classList.toggle('hidden', name !== 'automations');
  if (name === 'logs') loadLogs();
  if (name === 'settings') loadSettings();
}
document.getElementById('tabs').addEventListener('click', e => {
  const btn = e.target.closest('button[data-tab]');
  if (btn) showTab(btn.dataset.tab);
});

/* ---------------- Metrics + status ---------------- */
async function loadMetrics() {
  try {
    const m = await api('/api/metrics');
    $('m-received').textContent = m.comments_received;
    $('m-delivered').textContent = m.dms_delivered;
    $('m-follower').textContent = m.follower_dms;
    $('m-nonfollower').textContent = m.nonfollower_dms;
    $('m-failed').textContent = m.failed;
    $('m-duplicates').textContent = m.duplicates;
  } catch (e) {}
}

async function loadStatus() {
  try {
    const s = await api('/api/status');
    $('dot-token').style.background = s.token_valid ? 'var(--success)' : 'var(--danger)';
    $('token-label').textContent = s.token_valid ? 'Token Valid' : 'Token Invalid';
  } catch (e) {}
}

/* ---------------- Automations list ---------------- */
let automationsCache = [];
function automationCard(a) {
  const triggerLabel = a.trigger_type === 'ALL' ? 'Every comment' : 'Keywords: ' + (a.keywords || '—');
  const targetLabel = a.target_media === 'ALL' ? 'All posts' : (a.target_media_caption ? a.target_media_caption.slice(0, 40) : 'Selected post');
  return `
  <div class="card p-4 sm:p-5 flex items-center gap-4">
    <div class="w-12 h-12 rounded-xl bg-[var(--field)] shrink-0 overflow-hidden flex items-center justify-center">
      ${a.target_media_thumb ? `<img src="${esc(a.target_media_thumb)}" class="w-full h-full object-cover">` : `<span class="text-lg">🎯</span>`}
    </div>
    <div class="flex-1 min-w-0">
      <p class="font-semibold text-sm truncate">${esc(a.name)}</p>
      <p class="hint truncate">${esc(targetLabel)} · ${esc(triggerLabel)}</p>
    </div>
    <label class="flex items-center gap-2 shrink-0">
      <input type="checkbox" class="switch automation-toggle" data-id="${a.id}" ${a.active ? 'checked' : ''}>
    </label>
    <button class="btn-ghost edit-automation shrink-0" data-id="${a.id}">Edit</button>
    <button class="btn-ghost delete-automation shrink-0" data-id="${a.id}" style="color:var(--danger)">Delete</button>
  </div>`;
}

async function loadAutomations() {
  const wrap = $('tab-automations');
  try {
    const data = await api('/api/automations');
    automationsCache = data.automations;
    if (!automationsCache.length) {
      wrap.innerHTML = `<div class="card p-10 text-center">
        <p class="font-semibold mb-1">No automations yet</p>
        <p class="hint">Create one to start turning comments into DMs.</p>
      </div>`;
      return;
    }
    wrap.innerHTML = automationsCache.map(automationCard).join('');
  } catch (e) {
    wrap.innerHTML = `<div class="card p-6 hint">Couldn't load automations.</div>`;
  }
}

document.getElementById('tab-automations').addEventListener('click', async e => {
  const toggle = e.target.closest('.automation-toggle');
  const editBtn = e.target.closest('.edit-automation');
  const delBtn = e.target.closest('.delete-automation');
  if (toggle) {
    try { await api(`/api/automations/${toggle.dataset.id}/toggle`, {method: 'POST'}); loadMetrics(); }
    catch (e) { toast('Could not update automation', true); toggle.checked = !toggle.checked; }
  }
  if (editBtn) openWizard(automationsCache.find(a => a.id == editBtn.dataset.id));
  if (delBtn) {
    if (!confirm('Delete this automation? This cannot be undone.')) return;
    try { await api(`/api/automations/${delBtn.dataset.id}`, {method: 'DELETE'}); loadAutomations(); loadMetrics(); toast('Automation deleted'); }
    catch (e) { toast('Could not delete automation', true); }
  }
});

/* ---------------- Logs ---------------- */
async function loadLogs(status) {
  status = status || document.querySelector('#log-filters button.active')?.dataset.status || 'all';
  try {
    const data = await api('/api/logs?status=' + status);
    const body = $('logs-body');
    $('logs-empty').classList.toggle('hidden', data.logs.length > 0);
    body.innerHTML = data.logs.map(l => `
      <tr class="border-b last:border-0" style="border-color:var(--border)">
        <td class="px-4 py-3 hint font-mono text-xs whitespace-nowrap">${esc(l.created_at)}</td>
        <td class="px-4 py-3 font-medium">@${esc(l.commenter_username)}</td>
        <td class="px-4 py-3 max-w-[260px] truncate" title="${esc(l.comment_text)}">${esc(l.comment_text)}</td>
        <td class="px-4 py-3 hint">${esc(l.automation_name || '—')}</td>
        <td class="px-4 py-3">${l.is_follower === null ? '<span class="hint">—</span>' : (l.is_follower ? '✅' : '➖')}</td>
        <td class="px-4 py-3"><span class="badge badge-${esc(l.dm_status)}">${esc(l.dm_status)}</span></td>
      </tr>`).join('');
  } catch (e) {}
}
document.getElementById('log-filters').addEventListener('click', e => {
  const btn = e.target.closest('button[data-status]');
  if (!btn) return;
  document.querySelectorAll('#log-filters button').forEach(b => b.classList.toggle('active', b === btn));
  loadLogs(btn.dataset.status);
});

/* ---------------- Settings ---------------- */
async function loadSettings() {
  try {
    const s = await api('/api/settings');
    $('set-app-id').value = s.ig_app_id || '';
    $('set-app-secret').placeholder = s.ig_app_secret_set ? 'Saved (' + s.ig_app_secret_masked + ')' : 'Leave blank to keep current';
    $('set-token').placeholder = s.access_token_set ? 'Saved (' + s.access_token_masked + ')' : 'Leave blank to keep current';
    $('webhook-url').value = s.webhook_url;
    $('verify-token').value = s.verify_token;
    $('connected-preview').classList.toggle('hidden', !s.connected);
    if (s.connected) {
      $('conn-username').textContent = s.ig_username;
      $('conn-pic').src = s.ig_profile_pic || '';
    }
  } catch (e) {}
}

$('toggle-token').addEventListener('click', () => {
  const el = $('set-token');
  el.type = el.type === 'password' ? 'text' : 'password';
});

$('save-settings-btn').addEventListener('click', async () => {
  const btn = $('save-settings-btn');
  $('settings-error').classList.add('hidden');
  btn.disabled = true; btn.textContent = 'Saving…';
  try {
    const data = await api('/api/settings', {
      method: 'POST',
      body: JSON.stringify({
        ig_app_id: $('set-app-id').value,
        ig_app_secret: $('set-app-secret').value,
        access_token: $('set-token').value,
      })
    });
    if (data.connect_error) {
      $('settings-error').textContent = data.connect_error;
      $('settings-error').classList.remove('hidden');
    } else {
      toast(data.connected ? 'Connected to @' + data.ig_username : 'Settings saved');
    }
    $('set-app-secret').value = ''; $('set-token').value = '';
    loadSettings(); loadStatus();
  } catch (e) {
    $('settings-error').textContent = e.message; $('settings-error').classList.remove('hidden');
  }
  btn.disabled = false; btn.textContent = 'Save & connect';
});

$('oauth-btn').addEventListener('click', () => { window.location.href = '/oauth/start'; });

$('test-conn-btn').addEventListener('click', async () => {
  const btn = $('test-conn-btn');
  btn.disabled = true; btn.textContent = 'Testing…';
  try {
    const data = await api('/api/settings/test', {method: 'POST'});
    toast(data.valid ? 'Token is valid — @' + data.ig_username : (data.error || 'Token invalid'), !data.valid);
    loadStatus();
  } catch (e) { toast('Test failed', true); }
  btn.disabled = false; btn.textContent = 'Test connection';
});

document.querySelectorAll('[data-copy]').forEach(btn => {
  btn.addEventListener('click', () => {
    const input = $(btn.dataset.copy);
    input.select(); input.setSelectionRange(0, 99999);
    navigator.clipboard?.writeText(input.value);
    toast('Copied to clipboard');
  });
});

/* ---------------- Wizard ---------------- */
let wizardStep = 1;
let wizardKeywords = [];
let editingId = null;
let selectedMedia = null;

function renderStepDots() {
  document.querySelectorAll('[data-step-dot]').forEach(dot => {
    const n = parseInt(dot.dataset.stepDot);
    dot.classList.remove('done', 'current', 'todo');
    dot.classList.add(n < wizardStep ? 'done' : n === wizardStep ? 'current' : 'todo');
    dot.textContent = n < wizardStep ? '✓' : n;
  });
  document.querySelectorAll('.wizard-step').forEach(s => s.classList.toggle('hidden', parseInt(s.dataset.step) !== wizardStep));
  $('w-back').style.visibility = wizardStep === 1 ? 'hidden' : 'visible';
  $('w-next').textContent = wizardStep === 4 ? 'Accept' : 'Next';
}

function renderKeywordChips() {
  const wrap = $('keyword-chips');
  wrap.querySelectorAll('.chip').forEach(c => c.remove());
  wizardKeywords.forEach((kw, i) => {
    const chip = document.createElement('span');
    chip.className = 'chip';
    chip.innerHTML = `${esc(kw)} <span class="chip-x" data-i="${i}">✕</span>`;
    wrap.insertBefore(chip, $('keyword-input'));
  });
}
$('keyword-input').addEventListener('keydown', e => {
  if ((e.key === 'Enter' || e.key === ',') && e.target.value.trim()) {
    e.preventDefault();
    wizardKeywords.push(e.target.value.trim().replace(/,$/, ''));
    e.target.value = '';
    renderKeywordChips();
  } else if (e.key === 'Backspace' && !e.target.value && wizardKeywords.length) {
    wizardKeywords.pop();
    renderKeywordChips();
  }
});
$('keyword-chips').addEventListener('click', e => {
  const x = e.target.closest('.chip-x');
  if (x) { wizardKeywords.splice(parseInt(x.dataset.i), 1); renderKeywordChips(); }
});

document.querySelectorAll('input[name="w-trigger"]').forEach(r => {
  r.addEventListener('change', () => $('keywords-wrap').classList.toggle('hidden', r.value !== 'KEYWORDS' || !r.checked));
});
$('w-public-reply-enabled').addEventListener('change', e => $('public-reply-wrap').classList.toggle('hidden', !e.target.checked));
$('w-apply-all').addEventListener('change', e => {
  document.getElementById('media-grid').classList.toggle('opacity-40', e.target.checked);
  document.getElementById('media-grid').classList.toggle('pointer-events-none', e.target.checked);
  if (e.target.checked) selectedMedia = null;
});

async function loadMediaGrid() {
  const grid = $('media-grid');
  grid.innerHTML = '';
  $('media-status').textContent = 'Loading recent posts…';
  try {
    const data = await api('/api/media');
    if (!data.media || !data.media.length) {
      $('media-status').textContent = 'No media found — you can still target "All posts".';
      return;
    }
    $('media-status').textContent = '';
    grid.innerHTML = data.media.map(m => {
      const thumb = m.thumbnail_url || m.media_url || '';
      return `<div class="media-tile" data-id="${esc(m.id)}" data-thumb="${esc(thumb)}" data-caption="${esc((m.caption||'').slice(0,120))}">
        <img src="${esc(thumb)}" loading="lazy">
      </div>`;
    }).join('');
  } catch (e) {
    $('media-status').textContent = e.message || 'Connect your Instagram account in Settings to select a post.';
  }
}
$('media-grid').addEventListener('click', e => {
  const tile = e.target.closest('.media-tile');
  if (!tile) return;
  document.querySelectorAll('.media-tile').forEach(t => t.classList.remove('selected'));
  tile.classList.add('selected');
  selectedMedia = {id: tile.dataset.id, thumb: tile.dataset.thumb, caption: tile.dataset.caption};
});

function resetWizard() {
  wizardStep = 1; wizardKeywords = []; editingId = null; selectedMedia = null;
  $('w-name').value = ''; $('w-msg-follower').value = ''; $('w-msg-nonfollower').value = '';
  $('w-public-reply-text').value = ''; $('w-public-reply-enabled').checked = false;
  $('public-reply-wrap').classList.add('hidden');
  $('w-apply-all').checked = true;
  document.querySelector('input[name="w-trigger"][value="ALL"]').checked = true;
  $('keywords-wrap').classList.add('hidden');
  renderKeywordChips();
  $('wizard-error').classList.add('hidden');
  $('wizard-title').textContent = 'New automation';
}

function openWizard(existing) {
  resetWizard();
  if (existing) {
    editingId = existing.id;
    $('wizard-title').textContent = 'Edit automation';
    $('w-name').value = existing.name;
    $('w-msg-follower').value = existing.message_follower;
    $('w-msg-nonfollower').value = existing.message_nonfollower;
    $('w-public-reply-enabled').checked = existing.public_reply_enabled;
    $('w-public-reply-text').value = existing.public_reply_text || '';
    $('public-reply-wrap').classList.toggle('hidden', !existing.public_reply_enabled);
    document.querySelector(`input[name="w-trigger"][value="${existing.trigger_type}"]`).checked = true;
    $('keywords-wrap').classList.toggle('hidden', existing.trigger_type !== 'KEYWORDS');
    wizardKeywords = (existing.keywords || '').split(',').map(k => k.trim()).filter(Boolean);
    renderKeywordChips();
    if (existing.target_media !== 'ALL') {
      $('w-apply-all').checked = false;
      selectedMedia = {id: existing.target_media, thumb: existing.target_media_thumb, caption: existing.target_media_caption};
    }
  }
  renderStepDots();
  loadMediaGrid();
  $('wizard-backdrop').classList.remove('hidden');
}
function closeWizard() { $('wizard-backdrop').classList.add('hidden'); }

$('new-automation-btn').addEventListener('click', () => openWizard(null));
$('w-cancel').addEventListener('click', closeWizard);
$('wizard-backdrop').addEventListener('click', e => { if (e.target === $('wizard-backdrop')) closeWizard(); });

function validateStep(n) {
  $('wizard-error').classList.add('hidden');
  if (n === 1 && !$('w-name').value.trim()) return 'Give this automation a name';
  if (n === 2) {
    const triggerType = document.querySelector('input[name="w-trigger"]:checked').value;
    if (triggerType === 'KEYWORDS' && !wizardKeywords.length) return 'Add at least one keyword';
  }
  if (n === 3 && (!$('w-msg-follower').value.trim() || !$('w-msg-nonfollower').value.trim())) return 'Both messages are required';
  return null;
}

$('w-back').addEventListener('click', () => { if (wizardStep > 1) { wizardStep--; renderStepDots(); } });
$('w-next').addEventListener('click', async () => {
  const err = validateStep(wizardStep);
  if (err) { $('wizard-error').textContent = err; $('wizard-error').classList.remove('hidden'); return; }
  if (wizardStep < 4) { wizardStep++; renderStepDots(); return; }

  const payload = {
    name: $('w-name').value.trim(),
    target_media: $('w-apply-all').checked || !selectedMedia ? 'ALL' : selectedMedia.id,
    target_media_thumb: $('w-apply-all').checked || !selectedMedia ? '' : selectedMedia.thumb,
    target_media_caption: $('w-apply-all').checked || !selectedMedia ? '' : selectedMedia.caption,
    trigger_type: document.querySelector('input[name="w-trigger"]:checked').value,
    keywords: wizardKeywords.join(', '),
    message_follower: $('w-msg-follower').value.trim(),
    message_nonfollower: $('w-msg-nonfollower').value.trim(),
    public_reply_enabled: $('w-public-reply-enabled').checked,
    public_reply_text: $('w-public-reply-text').value.trim(),
    active: true,
  };

  const btn = $('w-next');
  btn.disabled = true; btn.textContent = 'Saving…';
  try {
    if (editingId) await api(`/api/automations/${editingId}`, {method: 'PUT', body: JSON.stringify(payload)});
    else await api('/api/automations', {method: 'POST', body: JSON.stringify(payload)});
    closeWizard();
    loadAutomations(); loadMetrics();
    toast(editingId ? 'Automation updated' : 'Automation created');
  } catch (e) {
    $('wizard-error').textContent = e.message; $('wizard-error').classList.remove('hidden');
  }
  btn.disabled = false; btn.textContent = wizardStep === 4 ? 'Accept' : 'Next';
});

/* ---------------- Logout ---------------- */
$('logout-btn').addEventListener('click', async () => {
  await api('/api/logout', {method: 'POST'});
  window.location.href = '/login';
});

/* ---------------- Boot ---------------- */
(function boot() {
  const params = new URLSearchParams(window.location.search);
  if (params.get('tab') === 'settings') showTab('settings');
  const oauth = params.get('oauth');
  if (oauth === 'success') toast('Instagram account connected');
  if (oauth && oauth !== 'success') toast('Connection failed — check App ID/Secret and try again', true);

  loadAutomations();
  loadMetrics();
  loadStatus();
  setInterval(loadMetrics, 15000);
  setInterval(loadStatus, 20000);
})();
</script>
</body>
</html>
"""

# ---------------------------------------------------------------------------
# Local dev entrypoint. Under PythonAnywhere's WSGI, this block never runs —
# the WSGI file just does: from flask_app import app as application
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)