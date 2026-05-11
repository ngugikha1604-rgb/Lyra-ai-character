"""
routes/admin.py — Admin/debug routes, yêu cầu X-Admin-Key header.

  GET /reset          — xóa session, reload AI (giữ memory.db)
  GET /reset-all      — xóa session + memory.db
  GET /secret/diary   — xem diary nội tâm của Lyra (nếu có)
"""

from __future__ import annotations

import os
import traceback

from flask import Blueprint, jsonify, request, session, current_app

from app.middleware import require_auth
from memory import DB_PATH

bp = Blueprint("admin", __name__)


def _replace_lyra_ai(new_ai):
    current_app.lyra_ai = new_ai
    current_app.stream_service._lyra_ai = new_ai
    current_app.stream_service._ai_lock = current_app.ai_chat_lock
    current_app.stream_service.reset_greeted_set()
    with current_app.stream_service._pool_lock:
        current_app.stream_service._new_viewer_pool.clear()
    current_app.proactive_service._lyra_ai = new_ai
    current_app.proactive_service._ai_lock = current_app.ai_chat_lock


@bp.route("/reset")
@require_auth
def reset():
    from core import MiniAI
    current_app._audit_logger.info(f"RESET called from IP={request.remote_addr}")
    session.clear()
    _replace_lyra_ai(MiniAI())
    return "Session cleared (memory.db preserved)"


@bp.route("/reset-all")
@require_auth
def reset_all():
    from core import MiniAI
    current_app._audit_logger.warning(
        f"RESET_ALL called from IP={request.remote_addr} — memory.db will be deleted"
    )
    session.clear()
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    _replace_lyra_ai(MiniAI())
    return "All cleared (session + memory)"


@bp.route("/secret/diary")
@require_auth
def secret_diary():
    """Trả về monologue diary entries của Lyra từ DB."""
    try:
        import sqlite3
        from memory import DB_LOCK
        if not os.path.exists(DB_PATH):
            return jsonify({"diary": []})
        with DB_LOCK:
            conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT content, timestamp FROM diaries ORDER BY id DESC LIMIT 20"
            ).fetchall()
            conn.close()
        return jsonify({"diary": [{"content": r[0], "created_at": r[1]} for r in rows]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
