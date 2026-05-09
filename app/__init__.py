"""
app/__init__.py — Application factory.

Sử dụng:
    from app import create_app
    app = create_app()
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import timedelta

from dotenv import load_dotenv
from flask import Flask
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_session import Session

load_dotenv()


def create_app() -> Flask:
    app = Flask(
        __name__,
        template_folder=os.path.join(os.path.dirname(__file__), "..", "templates"),
        static_folder=os.path.join(os.path.dirname(__file__), "..", "static"),
    )
    _configure_app(app)
    _setup_logging(app)
    _init_dependencies(app)
    _register_blueprints(app)
    _apply_middleware(app)
    return app


# ── 1. Flask config ──────────────────────────────────────────────────────────

def _configure_app(app: Flask) -> None:
    from config import FLASK_SECRET_KEY

    is_prod = os.environ.get("FLASK_ENV", "development") == "production"
    if not is_prod:
        os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")

    app.secret_key = FLASK_SECRET_KEY
    app.config.update(
        SESSION_COOKIE_SECURE     = is_prod,
        SESSION_COOKIE_HTTPONLY   = True,
        SESSION_COOKIE_SAMESITE   = "Lax",
        MAX_CONTENT_LENGTH        = 16 * 1024 * 1024,
        SESSION_TYPE              = "filesystem",
        SESSION_PERMANENT         = True,
        PERMANENT_SESSION_LIFETIME= timedelta(days=365),
        SESSION_FILE_DIR          = "./flask_sessions",
    )
    os.makedirs("./flask_sessions", exist_ok=True)
    Session(app)


# ── 2. Logging ───────────────────────────────────────────────────────────────

def _setup_logging(app: Flask) -> None:
    audit = logging.getLogger("lyra.audit")
    if not audit.handlers:
        h = logging.FileHandler("security_audit.log", encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        audit.addHandler(h)
        audit.setLevel(logging.INFO)
    app._audit_logger = audit


# ── 3. Dependencies ──────────────────────────────────────────────────────────

def _init_dependencies(app: Flask) -> None:
    from core import MiniAI
    from viewer_tracker import ViewerTracker, ChatPatternAnalyzer
    from youtube_chat import YouTubeChatPoller
    from vts_api import vts_bridge

    from app.services.audio_service     import audio_service
    from app.services.sse_service       import sse_service
    from app.services.stream_service    import stream_service
    from app.services.proactive_service import proactive_service
    from app.routes.auth import try_load_saved_credentials

    print("Initializing Lyra AI...")
    lyra_ai        = MiniAI()
    viewer_tracker = ViewerTracker()
    chat_analyzer  = ChatPatternAnalyzer()
    yt_poller      = YouTubeChatPoller(viewer_tracker=viewer_tracker)
    ai_chat_lock   = threading.Lock()
    print("Lyra AI initialized.")

    vts_bridge.start()

    limiter = Limiter(
        app=app,
        key_func=get_remote_address,
        default_limits=["200 per day", "50 per hour"],
    )

    stream_service.init(
        lyra_ai, viewer_tracker, chat_analyzer, yt_poller,
        vts_bridge, sse_service, audio_service, ai_chat_lock,
    )
    proactive_service.init(lyra_ai, sse_service, audio_service, ai_chat_lock)

    app.lyra_ai        = lyra_ai
    app.viewer_tracker = viewer_tracker
    app.chat_analyzer  = chat_analyzer
    app.yt_poller      = yt_poller
    app.ai_chat_lock   = ai_chat_lock
    app.vts_bridge     = vts_bridge
    app.limiter        = limiter
    app.audio_service  = audio_service
    app.sse_service    = sse_service
    app.stream_service = stream_service
    app.yt_credentials = try_load_saved_credentials()


# ── 4. Blueprints ─────────────────────────────────────────────────────────────

def _register_blueprints(app: Flask) -> None:
    from app.routes import (
        chat_bp, tts_bp, stream_bp,
        stream_events_bp, auth_bp, admin_bp,
    )

    app.limiter.limit("30 per minute")(chat_bp)
    app.limiter.limit("20 per minute")(tts_bp)
    app.limiter.limit("60 per minute")(stream_bp)

    for bp in (chat_bp, tts_bp, stream_bp, stream_events_bp, auth_bp, admin_bp):
        app.register_blueprint(bp)

    from flask import render_template, session

    @app.route("/")
    def index():
        session.permanent = True
        return render_template("index.html")

    @app.route("/favicon.ico")
    def favicon():
        return "", 204


# ── 5. Middleware ─────────────────────────────────────────────────────────────

def _apply_middleware(app: Flask) -> None:
    from app.middleware import apply_security_headers
    app.after_request(apply_security_headers)
