"""
routes/stream.py — Stream control routes.

  POST /stream/start           — bắt đầu stream, khởi động YouTube poller
  POST /stream/stop            — dừng stream
  GET  /stream/status          — trạng thái stream hiện tại
  GET  /stream/analytics       — viewer stats + queue snapshot
  GET  /stream/debug/queue     — debug priority queues (admin)
  POST /stream-chat            — nhận chat event thủ công qua HTTP (không qua YouTube poller)
  GET  /viewers                — top viewers
"""

from __future__ import annotations

import os
import traceback

from flask import Blueprint, jsonify, request, current_app

from app.helpers import build_state_payload, sanitize_input
from live_context import (
    set_stream_active,
    reset_live_context,
    load_live_context,
    update_field,
    record_donation,
    record_regular_arrival,
)

bp = Blueprint("stream", __name__)


# ------------------------------------------------------------------ #
# Stream greeting helper (background thread)                           #
# ------------------------------------------------------------------ #

def _broadcast_stream_greeting(lyra_ai, sse, ai_lock) -> None:
    """
    Gọi Gemini Flash để tạo lời chào mở màn, broadcast qua SSE.
    Chạy trong daemon thread — không block HTTP response.
    Fallback: Groq → hard-coded string.
    """
    import requests
    from config import (
        GEMINI_API_KEY, GEMINI_BASE_URL,
        GROQ_API_KEY, STRONG_BASE_URL, STRONG_MODEL,
        STREAM_TITLE,
    )
    from prompts import STREAM_GREETING_PROMPT

    # Dùng STREAM_GREETING_PROMPT có sẵn — đã có rule xưng hô 'em'/'mọi người'
    try:
        from config import STREAM_GAME, STREAM_GOALS, STREAM_NOTES
    except ImportError:
        STREAM_GAME = STREAM_GOALS = STREAM_NOTES = None

    user_prompt = STREAM_GREETING_PROMPT.format(
        title=STREAM_TITLE or "tâm sự tự do",
        game=STREAM_GAME   or "không có",
        goals=", ".join(STREAM_GOALS) if STREAM_GOALS else "không có",
        notes=STREAM_NOTES or "không có",
    )

    system_prompt = (
        "Bạn là Lyra, VTuber 16 tuổi người Việt: tinh nghịch, tỉnh bơ, thân thiện vừa đủ. "
        "Khi mở stream, Lyra xưng 'em' và gọi khán giả là 'mọi người'. "
        "Viết tiếng Việt đời thường, đúng 1 câu ngắn, văn bản thuần, không emoji."
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user",   "content": user_prompt},
    ]
    payload_body = {
        "messages":    messages,
        "temperature": 0.7,
        "max_tokens":  40,
    }

    greeting_text = None

    # ── Thử Gemini Flash trước (nhanh nhất) ─────────────────────────────
    if GEMINI_API_KEY:
        try:
            resp = requests.post(
                GEMINI_BASE_URL,
                headers={
                    "Authorization": f"Bearer {GEMINI_API_KEY}",
                    "Content-Type":  "application/json",
                },
                json={"model": "gemini-2.0-flash", **payload_body},
                timeout=8,
            )
            if resp.ok:
                greeting_text = resp.json()["choices"][0]["message"]["content"].strip()
                print(f"[StreamGreeting] Gemini OK: {greeting_text[:60]}")
        except Exception as e:
            print(f"[StreamGreeting] Gemini failed: {e}")

    # ── Fallback: Groq ────────────────────────────────────────────
    if not greeting_text and GROQ_API_KEY:
        try:
            resp = requests.post(
                STRONG_BASE_URL,
                headers={
                    "Authorization": f"Bearer {GROQ_API_KEY}",
                    "Content-Type":  "application/json",
                },
                json={"model": STRONG_MODEL, **payload_body},
                timeout=8,
            )
            if resp.ok:
                greeting_text = resp.json()["choices"][0]["message"]["content"].strip()
                print(f"[StreamGreeting] Groq OK: {greeting_text[:60]}")
        except Exception as e:
            print(f"[StreamGreeting] Groq failed: {e}")

    # ── Fallback cuối: hard-coded ──────────────────────────────────
    if not greeting_text:
        greeting_text = "Stream bắt đầu rồi nha mọi người, nhắn tin cho Lyra đi!"
        print("[StreamGreeting] Using hard-coded fallback")

    # Broadcast qua SSE
    try:
        with ai_lock:
            emotion = lyra_ai.emotion_from_state() if hasattr(lyra_ai, "emotion_from_state") else "happy"
            affection_val = int(round(lyra_ai.emotion.affection))
            mood_val = int(round(lyra_ai.emotion.mood))
        sse.broadcast({
            "reply":       greeting_text,
            "monologue":   "",
            "emotion":     emotion,
            "action":      "GREET",
            "source_type": "system",
            "sender_name": "Lyra",
            "affection":   affection_val,
            "mood":        mood_val,
        })
        print("[StreamGreeting] Broadcast OK")
    except Exception as e:
        print(f"[StreamGreeting] Broadcast error: {e}")


# ------------------------------------------------------------------ #
# POST /stream/start                                                   #
# ------------------------------------------------------------------ #

@bp.route("/stream/start", methods=["POST"])
def stream_start():
    lyra_ai        = current_app.lyra_ai
    yt_poller      = current_app.yt_poller
    stream_service = current_app.stream_service

    data       = request.get_json(silent=True) or {}
    video_id   = data.get("video_id", os.environ.get("YOUTUBE_VIDEO_ID", ""))
    chat_id    = data.get("chat_id", "")

    try:
        from youtube_chat import get_live_chat_id, get_current_live_stream_info
        from google.oauth2.credentials import Credentials

        creds_path = os.path.join(os.path.dirname(__file__), "..", "..", "youtube_credentials.json")
        creds_path = os.path.abspath(creds_path)

        if not os.path.exists(creds_path):
            return jsonify({"error": "No YouTube credentials found. Please authenticate first."}), 401

        import json as _json
        with open(creds_path) as f:
            creds_data = _json.load(f)

        credentials = Credentials(
            token         = creds_data.get("token"),
            refresh_token = creds_data.get("refresh_token"),
            token_uri     = creds_data.get("token_uri"),
            client_id     = creds_data.get("client_id"),
            client_secret = creds_data.get("client_secret"),
            scopes        = creds_data.get("scopes"),
        )

        # Nếu không có chat_id lẫn video_id → tự tìm stream đang active trên kênh
        if not chat_id and not video_id:
            try:
                video_id, chat_id = get_current_live_stream_info(credentials)
            except Exception as e:
                print(f"[Stream] get_current_live_stream_info failed: {e}")

        # Nếu có video_id nhưng chưa có chat_id → lấy từ video_id
        if not chat_id and video_id:
            try:
                chat_id = get_live_chat_id(credentials, video_id)
            except Exception as e:
                print(f"[Stream] get_live_chat_id failed: {e}")

        if not chat_id:
            return jsonify({"error": "Không tìm thấy stream nào đang active. Hãy bắt đầu stream trên YouTube trước."}), 400

        yt_poller.start(credentials, chat_id)
        lyra_ai.is_streaming = True
        stream_service.reset_greeted_set()
        set_stream_active(True)

        # Lưu thời điểm bắt đầu stream để tính duration khi stop
        from memory_utils import get_now_vn
        update_field("stream_start_time", get_now_vn().isoformat())

        # Lấy platform/channel_id để khởi động promote timer
        try:
            from config import YOUTUBE_CHANNEL_ID
            _platform   = "youtube"
            _channel_id = YOUTUBE_CHANNEL_ID or "default"
        except Exception:
            _platform   = "youtube"
            _channel_id = "default"
        stream_service.start_promote_timer(_platform, _channel_id)
        # Bootstrap L3 context from Pinecone (background to avoid blocking HTTP)
        from background_worker import enqueue, PRIORITY_HIGH
        enqueue(PRIORITY_HIGH, lyra_ai.memory.bootstrap_stream_context)

        # Ghi video_id vào live context để persist qua restart
        if video_id:
            try:
                from live_context import update_stream_info
                update_stream_info(video_id=video_id, chat_id=chat_id)
            except Exception:
                pass

        # Set timer → proactive sẽ kích hoạt sau 2 phút im lặng
        lyra_ai._last_viewer_message_time = get_now_vn()

        # Greeting runs in the centralized background worker.
        sse     = current_app.sse_service
        ai_lock = current_app.ai_chat_lock
        enqueue(PRIORITY_HIGH, _broadcast_stream_greeting, lyra_ai, sse, ai_lock)

        print(f"[Stream] Started — video_id={video_id} chat_id={chat_id}")
        return jsonify({"ok": True, "chat_id": chat_id, "video_id": video_id})

    except Exception:
        traceback.print_exc()
        return jsonify({"error": "Failed to start stream"}), 500


# ------------------------------------------------------------------ #
# Stream farewell helper (background thread)                           #
# ------------------------------------------------------------------ #

def _broadcast_stream_farewell(lyra_ai, sse, ai_lock, viewer_tracker, platform, channel_id) -> None:
    """
    Gọi LLM để tạo lời tạm biệt cuối stream, broadcast qua SSE.
    Chạy trong background worker — không block HTTP response.
    Fallback: Groq → hard-coded string.
    """
    import requests
    from datetime import datetime
    from config import (
        GEMINI_API_KEY, GEMINI_BASE_URL,
        GROQ_API_KEY, STRONG_BASE_URL, STRONG_MODEL,
    )
    from prompts import STREAM_FAREWELL_PROMPT
    from live_context import load_live_context

    # Tính duration stream
    try:
        ctx = load_live_context()
        start_iso = ctx.get("stream_start_time", "")
        if start_iso:
            from memory_utils import get_now_vn
            delta = get_now_vn() - datetime.fromisoformat(start_iso)
            total_mins = int(delta.total_seconds() // 60)
            hours, mins = divmod(total_mins, 60)
            duration = f"{hours} tiếng {mins} phút" if hours else f"{mins} phút"
        else:
            duration = "một lúc"
    except Exception:
        duration = "một lúc"

    # Top 3 viewers
    try:
        top = viewer_tracker.get_top_viewers(platform=platform, channel_id=channel_id, limit=3)
        top_names = ", ".join([v["viewer_name"] for v in top if v.get("viewer_name")]) or "mọi người"
    except Exception:
        top_names = "mọi người"

    # Rolling summary từ lyra_ai memory (nếu có)
    try:
        summary = lyra_ai.memory._rolling_stream_summary or "một buổi stream vui"
    except Exception:
        summary = "một buổi stream vui"

    user_prompt = STREAM_FAREWELL_PROMPT.format(
        summary=summary,
        top_viewers=top_names,
        duration=duration,
    )

    system_prompt = (
        "Bạn là Lyra, VTuber 16 tuổi người Việt: tinh nghịch, tỉnh bơ, thân thiện vừa đủ. "
        "Khi kết stream, Lyra xưng 'em', cảm ơn 'mọi người', và vẫn giữ chất hơi xéo nhẹ. "
        "Viết tiếng Việt đời thường, đúng 1 câu ngắn, văn bản thuần, không emoji."
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user",   "content": user_prompt},
    ]
    payload_body = {
        "messages":    messages,
        "temperature": 0.7,
        "max_tokens":  50,
    }

    farewell_text = None

    # ── Thử Gemini Flash trước ───────────────────────────────────────────
    if GEMINI_API_KEY:
        try:
            resp = requests.post(
                GEMINI_BASE_URL,
                headers={
                    "Authorization": f"Bearer {GEMINI_API_KEY}",
                    "Content-Type":  "application/json",
                },
                json={"model": "gemini-2.0-flash", **payload_body},
                timeout=8,
            )
            if resp.ok:
                farewell_text = resp.json()["choices"][0]["message"]["content"].strip()
                print(f"[StreamFarewell] Gemini OK: {farewell_text[:60]}")
        except Exception as e:
            print(f"[StreamFarewell] Gemini failed: {e}")

    # ── Fallback: Groq ────────────────────────────────────────────────────
    if not farewell_text and GROQ_API_KEY:
        try:
            resp = requests.post(
                STRONG_BASE_URL,
                headers={
                    "Authorization": f"Bearer {GROQ_API_KEY}",
                    "Content-Type":  "application/json",
                },
                json={"model": STRONG_MODEL, **payload_body},
                timeout=8,
            )
            if resp.ok:
                farewell_text = resp.json()["choices"][0]["message"]["content"].strip()
                print(f"[StreamFarewell] Groq OK: {farewell_text[:60]}")
        except Exception as e:
            print(f"[StreamFarewell] Groq failed: {e}")

    # ── Fallback cuối: hard-coded ─────────────────────────────────────────
    if not farewell_text:
        farewell_text = "Cảm ơn mọi người đã xem stream hôm nay, hẹn gặp lại nhé!"
        print("[StreamFarewell] Using hard-coded fallback")

    # Broadcast qua SSE
    try:
        with ai_lock:
            emotion = lyra_ai.emotion_from_state() if hasattr(lyra_ai, "emotion_from_state") else "content"
            affection_val = int(round(lyra_ai.emotion.affection))
            mood_val = int(round(lyra_ai.emotion.mood))
        sse.broadcast({
            "reply":       farewell_text,
            "monologue":   "",
            "emotion":     emotion,
            "action":      "WAVE",
            "source_type": "system",
            "sender_name": "Lyra",
            "affection":   affection_val,
            "mood":        mood_val,
        })
        print("[StreamFarewell] Broadcast OK")
    except Exception as e:
        print(f"[StreamFarewell] Broadcast error: {e}")
