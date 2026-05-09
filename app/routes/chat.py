"""
routes/chat.py — Các route liên quan đến chat với Lyra.

  POST /chat         — owner chat qua web UI
  GET  /proactive    — Lyra chủ động nhắn khi user vắng lâu
  GET  /history      — lịch sử chat từ DB
  GET  /status       — AI state hiện tại
  GET  /session-info — debug: internal AI state
  GET  /analytics    — analytics từ DB
"""

from __future__ import annotations

import os
import traceback
from datetime import datetime

import pytz
from flask import Blueprint, jsonify, request, session, current_app

from app.helpers import build_state_payload, sanitize_input
from memory import DB_PATH, DB_LOCK
import sqlite3

bp = Blueprint("chat", __name__)


def _get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=20.0)
    conn.row_factory = sqlite3.Row
    return conn


# ------------------------------------------------------------------ #
# POST /chat                                                           #
# ------------------------------------------------------------------ #

@bp.route("/chat", methods=["POST"])
def chat():
    lyra_ai      = current_app.lyra_ai
    ai_chat_lock = current_app.ai_chat_lock
    audio        = current_app.audio_service
    vts_bridge   = current_app.vts_bridge
    limiter      = current_app.limiter

    try:
        session.permanent = True
        data = request.get_json()

        if not data or "message" not in data:
            return jsonify({"error": "Invalid request"}), 400

        user_input = sanitize_input(data["message"], max_length=1000)
        if not user_input:
            return jsonify({"reply": "Please say something."})

        # Ngắt audio đang phát (Action Interruption)
        audio.clear()
        vts_bridge.trigger_emotion("thinking")

        with ai_chat_lock:
            result = lyra_ai.chat(user_input, source_type="owner")

        print(
            "[CHAT] reply_len=%s monologue_len=%s emotion=%s action=%s"
            % (
                len((result or {}).get("reply", "") or ""),
                len((result or {}).get("monologue", "") or ""),
                (result or {}).get("emotion", ""),
                (result or {}).get("action", ""),
            )
        )
        return jsonify(build_state_payload(lyra_ai, result))

    except Exception:
        traceback.print_exc()
        return jsonify({
            "reply":        "Something went wrong...",
            "emotion":      "neutral",
            "affection":    50,
            "mood":         0,
            "time_period":  "afternoon",
            "time_gap_hours": None,
        })


# ------------------------------------------------------------------ #
# GET /proactive                                                       #
# ------------------------------------------------------------------ #

@bp.route("/proactive", methods=["GET"])
def proactive():
    lyra_ai      = current_app.lyra_ai
    ai_chat_lock = current_app.ai_chat_lock

    try:
        with ai_chat_lock:
            msg = lyra_ai.get_proactive_message()

        if not msg:
            return jsonify({"message": None, "should_show": False})

        lyra_ai.memory["time_tracking"]["last_message_time"] = datetime.now(
            pytz.timezone("Asia/Ho_Chi_Minh")
        ).isoformat()
        lyra_ai.memory._is_dirty = True
        lyra_ai.save_memory()

        payload = build_state_payload(lyra_ai)
        payload.update({"message": msg, "should_show": True})
        return jsonify(payload)

    except Exception:
        traceback.print_exc()
        return jsonify({"message": None, "should_show": False})


# ------------------------------------------------------------------ #
# GET /history                                                         #
# ------------------------------------------------------------------ #

@bp.route("/history", methods=["GET"])
def get_history():
    try:
        if not os.path.exists(DB_PATH):
            return jsonify({"history": []})
        with DB_LOCK:
            conn = _get_db()
            messages = [
                {"role": r[0], "content": r[1]}
                for r in conn.execute(
                    "SELECT role, content FROM conversation ORDER BY id ASC"
                )
            ]
            conn.close()
        return jsonify({"history": messages})
    except Exception:
        return jsonify({"history": []})


# ------------------------------------------------------------------ #
# GET /status                                                          #
# ------------------------------------------------------------------ #

@bp.route("/status")
def status():
    return jsonify(build_state_payload(current_app.lyra_ai))


# ------------------------------------------------------------------ #
# GET /session-info                                                    #
# ------------------------------------------------------------------ #

@bp.route("/session-info")
def session_info():
    ai = current_app.lyra_ai
    return jsonify({
        "message_count": len(ai.messages),
        "mood":          ai.mood,
        "affection":     ai.affection,
        "attention":     ai.attention,
    })


# ------------------------------------------------------------------ #
# GET /analytics                                                       #
# ------------------------------------------------------------------ #

@bp.route("/analytics")
def get_analytics():
    try:
        if not os.path.exists(DB_PATH):
            return jsonify({
                "emotions": {}, "moodHistory": [], "totalMessages": 0,
                "conversationCount": 0, "userName": "Not Set", "favoriteTopics": [],
            })
        with DB_LOCK:
            conn = _get_db()
            c = conn.cursor()
            name_row  = c.execute("SELECT value FROM profile WHERE key='name'").fetchone()
            total_row = c.execute("SELECT value FROM metadata WHERE key='total_messages'").fetchone()
            topics    = [
                r[0] for r in c.execute(
                    "SELECT value FROM facts WHERE type='topic' ORDER BY id DESC LIMIT 10"
                )
            ]
            conn.close()
        return jsonify({
            "emotions":          {},
            "moodHistory":       [],
            "totalMessages":     int(total_row[0]) if total_row else 0,
            "conversationCount": 0,
            "userName":          name_row[0] if name_row else "Not Set",
            "favoriteTopics":    topics,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500
