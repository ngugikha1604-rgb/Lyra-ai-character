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
        GROQ_API_KEY, TRANSLATE_BASE_URL, TRANSLATE_MODEL,
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
        "⚠️ XƯNG HÔ BẮT BUỘC: LUÔN xưng 'em', gọi 'anh/chị' hoặc 'mọi người'. "
        "TUYỆT ĐỐI KHÔNG dùng 'tôi', 'mình', 'bạn'. "
        "Bạn là Lyra, VTuber nữ 16 tuổi, giọng điệu ngây ngô dễ thương. "
        "Viết bằng tiếng Việt, không dùng emoji, trả về văn bản thuần."
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user",   "content": user_prompt},
    ]
    payload_body = {
        "messages":    messages,
        "temperature": 0.7,
        "max_tokens":  60,
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
                TRANSLATE_BASE_URL,
                headers={
                    "Authorization": f"Bearer {GROQ_API_KEY}",
                    "Content-Type":  "application/json",
                },
                json={"model": TRANSLATE_MODEL, **payload_body},
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
        sse.broadcast({
            "reply":       greeting_text,
            "monologue":   "",
            "emotion":     emotion,
            "action":      "GREET",
            "source_type": "system",
            "sender_name": "Lyra",
            "affection":   int(round(lyra_ai.affection)),
            "mood":        int(round(lyra_ai.mood)),
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

    try:
        data       = request.get_json() or {}
        video_id   = data.get("video_id", os.environ.get("YOUTUBE_VIDEO_ID", ""))
        chat_id    = data.get("chat_id",  os.environ.get("YOUTUBE_LIVE_CHAT_ID", ""))
        credentials = current_app.yt_credentials  # set bởi auth route

        if not credentials:
            return jsonify({"error": "YouTube OAuth credentials required. Visit /authorize first.", "authorize_url": "/authorize"}), 401

        # Thử refresh token nếu hết hạn trước khi dùng
        from app.routes.auth import _refresh_credentials, _creds_dict_to_object
        import google.oauth2.credentials
        creds_obj = _creds_dict_to_object(credentials)
        if creds_obj.expired or not creds_obj.valid:
            print("[Stream] Token hết hạn, đang refresh...")
            refreshed = _refresh_credentials(credentials)
            if not refreshed:
                # Xóa credentials cũ, yêu cầu re-authorize
                current_app.yt_credentials = None
                return jsonify({"error": "Token hết hạn và không thể refresh. Vui lòng re-authorize.", "authorize_url": "/authorize"}), 401
            credentials = refreshed
            current_app.yt_credentials = credentials

        # Nếu không có chat_id lẫn video_id → tự tìm stream đang active trên kênh
        if not chat_id and not video_id:
            from youtube_chat import get_current_live_stream_info
            video_id, chat_id = get_current_live_stream_info(credentials)

        # Nếu có video_id nhưng chưa có chat_id → lấy từ video_id
        if not chat_id and video_id:
            from youtube_chat import get_live_chat_id
            chat_id = get_live_chat_id(credentials, video_id)

        if not chat_id:
            return jsonify({"error": "Không tìm thấy stream nào đang active. Hãy bắt đầu stream trên YouTube trước."}), 400

        yt_poller.start(credentials, chat_id)
        lyra_ai.is_streaming = True
        stream_service.reset_greeted_set()
        set_stream_active(True)

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
        from memory_utils import get_now_vn
        lyra_ai._last_viewer_message_time = get_now_vn()

        # Greeting chạy background — trả 200 ngay, broadcast SSE sau ~1-2s
        import threading
        sse     = current_app.sse_service
        ai_lock = current_app.ai_chat_lock
        threading.Thread(
            target=_broadcast_stream_greeting,
            args=(lyra_ai, sse, ai_lock),
            daemon=True,
            name="StreamGreeting",
        ).start()

        print(f"[Stream] Started — video_id={video_id} chat_id={chat_id}")
        return jsonify({"ok": True, "chat_id": chat_id, "video_id": video_id})

    except Exception:
        traceback.print_exc()
        return jsonify({"error": "Failed to start stream"}), 500


# ------------------------------------------------------------------ #
# POST /stream/stop                                                    #
# ------------------------------------------------------------------ #

from background_worker import enqueue, PRIORITY_NORMAL, PRIORITY_HIGH


def _stream_stop_cleanup(viewer_tracker, platform: str, channel_id: str, lyra_ai) -> None:
    """
    Chạy trong background worker sau khi stream stop.
    Thứ tự: promote → clear stats → diary.
    Tất cả trong 1 task để đảm bảo sequential, không race.
    """
    try:
        promoted = viewer_tracker.promote_regular_viewers(platform, channel_id)
        print(f"[StreamCleanup] Promoted {len(promoted)} viewer(s) to regular.")
    except Exception as e:
        print(f"[StreamCleanup] promote error: {e}")

    try:
        viewer_tracker.clear_session_stats(platform, channel_id)
        print("[StreamCleanup] Session stats cleared.")
    except Exception as e:
        print(f"[StreamCleanup] clear_session_stats error: {e}")

    try:
        lyra_ai.write_diary_entry()
    except Exception as e:
        print(f"[StreamCleanup] diary error: {e}")


@bp.route("/stream/stop", methods=["POST"])
def stream_stop():
    lyra_ai        = current_app.lyra_ai
    yt_poller      = current_app.yt_poller
    stream_service = current_app.stream_service
    viewer_tracker = current_app.viewer_tracker

    yt_poller.stop()
    stream_service.stop_promote_timer()   # dừng 30-min timer
    lyra_ai.is_streaming = False
    stream_service.reset_greeted_set()
    set_stream_active(False)

    # Lấy platform/channel_id từ config
    try:
        from config import YOUTUBE_CHANNEL_ID
        platform   = "youtube"
        channel_id = YOUTUBE_CHANNEL_ID or "default"
    except Exception:
        platform   = "youtube"
        channel_id = "default"

    # Enqueue cleanup: promote → clear → diary (sequential, 1 task)
    enqueue(PRIORITY_NORMAL, _stream_stop_cleanup, viewer_tracker, platform, channel_id, lyra_ai)

    print("[Stream] Stopped — cleanup enqueued.")
    return jsonify({"ok": True})


# ------------------------------------------------------------------ #
# GET /stream/status                                                   #
# ------------------------------------------------------------------ #

@bp.route("/stream/status")
def stream_status():
    lyra_ai   = current_app.lyra_ai
    yt_poller = current_app.yt_poller
    return jsonify({
        "is_streaming": lyra_ai.is_streaming,
        "poller_running": yt_poller._is_running,
        "live_context":  load_live_context(),
    })


# ------------------------------------------------------------------ #
# GET /stream/analytics                                               #
# ------------------------------------------------------------------ #

@bp.route("/stream/analytics")
def stream_analytics():
    viewer_tracker  = current_app.viewer_tracker
    stream_service  = current_app.stream_service

    top = viewer_tracker.get_top_viewers(limit=20)
    return jsonify({
        "top_viewers":    top,
        "queue_snapshot": stream_service.get_queue_snapshot(),
    })


# ------------------------------------------------------------------ #
# GET /stream/debug/queue  (admin)                                    #
# ------------------------------------------------------------------ #

@bp.route("/stream/debug/queue")
def debug_queue():
    from app.middleware import require_auth
    @require_auth
    def _inner():
        return jsonify(current_app.stream_service.get_queue_snapshot())
    return _inner()


# ------------------------------------------------------------------ #
# POST /stream-chat  (manual injection)                               #
# ------------------------------------------------------------------ #

@bp.route("/stream-chat", methods=["POST"])
def stream_chat():
    """
    Nhận chat event thủ công (không qua YouTube poller).
    Dùng cho: test, Discord bridge, custom platform webhook.
    """
    lyra_ai        = current_app.lyra_ai
    viewer_tracker = current_app.viewer_tracker
    chat_analyzer  = current_app.chat_analyzer
    ai_chat_lock   = current_app.ai_chat_lock
    vts_bridge     = current_app.vts_bridge
    stream_service = current_app.stream_service

    try:
        data = request.get_json()
        if not data or "message" not in data or "sender_id" not in data:
            return jsonify({"error": "Missing required fields: message, sender_id"}), 400

        message     = sanitize_input(data["message"], max_length=1000)
        sender_id   = str(data["sender_id"]).strip()
        sender_name = str(data.get("sender_name", "Viewer")).strip()
        channel_id  = str(data.get("channel_id", "default")).strip()
        platform    = str(data.get("platform", "unknown")).strip()
        role        = str(data.get("role", "viewer")).strip()

        if not message or not sender_id:
            return jsonify({"error": "Empty message or sender_id"}), 400

        # Đẩy vào priority queue — xử lý async bởi consumer loop
        stream_service.enqueue_event({
            "message":     message,
            "sender_id":   sender_id,
            "sender_name": sender_name,
            "channel_id":  channel_id,
            "platform":    platform,
            "role":        role,
            "is_donor":    data.get("is_donor", False),
            "donate_amount": data.get("donate_amount", ""),
            "gender":      data.get("gender", "male"),
        })

        return jsonify({"ok": True, "queued": True})

    except Exception:
        traceback.print_exc()
        return jsonify({"error": "Internal server error"}), 500


# ------------------------------------------------------------------ #
# GET /viewers                                                         #
# ------------------------------------------------------------------ #

@bp.route("/viewers")
def get_viewers():
    platform   = request.args.get("platform")
    channel_id = request.args.get("channel_id")
    limit      = min(int(request.args.get("limit", 10)), 50)
    top = current_app.viewer_tracker.get_top_viewers(
        platform=platform, channel_id=channel_id, limit=limit
    )
    return jsonify({"viewers": top, "count": len(top)})
