from flask import (
    Flask,
    render_template,
    request,
    jsonify,
    session,
    redirect,
    url_for,
)
from flask_session import Session
from core import MiniAI
from viewer_tracker import ViewerTracker, ChatPatternAnalyzer
from youtube_chat import YouTubeChatPoller, get_live_chat_id, get_current_live_stream_info
from datetime import timedelta, datetime
import traceback
import json
import os
import io
import sqlite3
import threading
import time
import requests
import pytz
from pydub import AudioSegment
import sounddevice as sd
import numpy as np
from memory import DB_PATH, DB_LOCK
from memory_utils import get_now_vn
from config import (
    ELEVENLABS_API_KEY,
    ELEVENLABS_VOICE_ID,
    FLASK_SECRET_KEY,
    FPT_API_KEY,
    FPT_TTS_URL,
    FPT_TTS_VOICE,
)
from config import STREAM_TITLE, STREAM_GAME, STREAM_GOALS, STREAM_NOTES
from config import (
    STREAM_REPLY_COOLDOWN,
    STREAM_NEW_VIEWER_INTERVAL,
    STREAM_REGULAR_MIN_MESSAGES,
)
from dotenv import load_dotenv
import google_auth_oauthlib.flow
from vts_api import vts_bridge
from live_context import (
    set_stream_active,
    record_donation,
    record_regular_arrival,
    record_milestone,
    update_chat_vibe,
    add_constraint,
    clear_constraint,
    reset_live_context,
    load_live_context,
)
from background_worker import enqueue, get_queue_stats, PRIORITY_CRITICAL, PRIORITY_HIGH, PRIORITY_NORMAL

load_dotenv()

# Đường dẫn tới file bạn tải từ Google Cloud
CLIENT_SECRETS_FILE = "client_secret.json"
SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl"]
YOUTUBE_CREDENTIALS_FILE = os.environ.get(
    "YOUTUBE_CREDENTIALS_FILE", "youtube_credentials.json"
)
DEFAULT_YOUTUBE_LIVE_CHAT_ID = os.environ.get("YOUTUBE_LIVE_CHAT_ID", "")
DEFAULT_YOUTUBE_VIDEO_ID = os.environ.get("YOUTUBE_VIDEO_ID", "")
def find_vb_cable_device():
    """Tự động tìm ID của thiết bị 'CABLE Input' (VB-Audio Virtual Cable)"""
    try:
        devices = sd.query_devices()
        for i, dev in enumerate(devices):
            # Tìm thiết bị có tên chứa 'CABLE Input' và có hỗ trợ output (từ góc nhìn của Python)
            if "CABLE Input" in dev['name'] and dev['max_output_channels'] > 0:
                print(f"[Audio] Đã tìm thấy VB-Cable tại ID: {i} ({dev['name']})")
                return i
    except Exception as e:
        print(f"[Audio] Lỗi khi quét thiết bị âm thanh: {e}")
    
    # Fallback về giá trị từ môi trường hoặc mặc định
    return int(os.environ.get("VB_CABLE_DEVICE_ID", "15"))

VB_CABLE_DEVICE_ID = find_vb_cable_device()

# Security configurations
# SESSION_COOKIE_SECURE=True chỉ dùng trên HTTPS — tắt khi dev local (HTTP)
_is_production = os.environ.get("FLASK_ENV", "development") == "production"

# Cho phép OAuth qua HTTP khi dev local (không dùng trên production)
if not _is_production:
    os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")

app = Flask(__name__)
app.secret_key = FLASK_SECRET_KEY

app.config["SESSION_COOKIE_SECURE"] = _is_production
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16MB max request size
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import functools

# CORS Protection - only allow localhost during development
@app.after_request
def after_request(response):
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    return response

# Rate Limiter
limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=["200 per day", "50 per hour"]
)

# Simple authentication decorator for sensitive endpoints
def require_auth(f):
    @functools.wraps(f)
    def decorated_function(*args, **kwargs):
        # Check for admin session or API key
        auth_header = request.headers.get('X-Admin-Key')
        if auth_header != os.environ.get('ADMIN_API_KEY', 'lyra-admin-key-change-me'):
            return jsonify({'error': 'Unauthorized'}), 401
        return f(*args, **kwargs)
    return decorated_function

# Input sanitization
def sanitize_input(text, max_length=1000):
    if not text or not isinstance(text, str):
        return ''
    import re
    # Remove potentially dangerous characters
    text = re.sub(r'[<>\"\'%;)(&+]', '', text)
    return text.strip()[:max_length]


def _save_youtube_credentials(credentials: dict) -> None:
    """Persist local OAuth credentials so stream controls survive browser session loss."""
    try:
        with open(YOUTUBE_CREDENTIALS_FILE, "w", encoding="utf-8") as f:
            json.dump(credentials, f)
    except Exception as e:
        print(f"[YouTube OAuth] Could not save credentials: {e}")


def _load_youtube_credentials() -> dict | None:
    try:
        with open(YOUTUBE_CREDENTIALS_FILE, "r", encoding="utf-8") as f:
            credentials = json.load(f)
        if isinstance(credentials, dict) and credentials.get("token"):
            return credentials
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as e:
        print(f"[YouTube OAuth] Credentials file corrupted, deleting: {e}")
        try:
            os.remove(YOUTUBE_CREDENTIALS_FILE)
        except Exception:
            pass
        return None
    except Exception as e:
        print(f"[YouTube OAuth] Could not load credentials: {e}")
    return None



# ========================
# SESSION CONFIGURATION
# ========================

app.config["SESSION_TYPE"] = "filesystem"
app.config["SESSION_PERMANENT"] = True
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=365)
app.config["SESSION_FILE_DIR"] = "./flask_sessions"

os.makedirs("./flask_sessions", exist_ok=True)

Session(app)

# DB_PATH is imported from memory

# ========================
# STREAM CONTENT CONTEXT
# ========================
# Đọc từ config.py — chỉnh STREAM_TITLE, STREAM_GAME, STREAM_GOALS, STREAM_NOTES trước khi stream.


def _build_stream_content_context() -> str:
    """Tạo string inject vào prompt - Đã loại bỏ bối cảnh game và mục tiêu."""
    lines = ["[STREAM CONTEXT]"]
    lines.append("Hôm nay Lyra stream chuyện phiếm, tâm sự với mọi người.")

    # Inject stream milestones để Lyra có thể reference tự nhiên
    try:
        milestones = lyra_ai.memory.get_stream_milestones(limit=3)
        if milestones:
            milestone_strs = [
                f"{m['description']} ({m['achieved_at'][:10]})" for m in milestones
            ]
            lines.append(f"Kỷ niệm stream: {' | '.join(milestone_strs)}")
    except Exception:
        pass

    lines.append("[/STREAM CONTEXT]")
    return "\n".join(lines)


def get_db():
    # Thêm check_same_thread=False để hỗ trợ môi trường Flask đa luồng
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=20.0)
    conn.row_factory = sqlite3.Row
    return conn


def build_state_payload(ai, result=None):
    # result contains: {reply, monologue, emotion, action, mood, affection, time_period, time_gap_hours}
    if result is None:
        result = {}

    return {
        "affection": int(round(ai.affection)),
        "mood": int(round(ai.mood)),
        "emotion": result.get("emotion") or ai.emotion_from_state(),
        "action": result.get("action") or "NONE",
        "monologue": result.get("monologue") or "",
        "reply": result.get("reply") or "",
        "rolling_stream_summary": ai.memory._rolling_stream_summary or "",
        "time_period": result.get("time_period")
        or getattr(ai, "time_period", "afternoon"),
        "time_gap_hours": result.get("time_gap_hours")
        if result.get("time_gap_hours") is not None
        else getattr(ai, "time_gap_hours", None),
    }


# ========================
# GLOBAL AI INSTANCE
# ========================
# Initialize MiniAI once globally to prevent DB loading bottleneck on every request.
print("Initializing Lyra AI (this may take a few seconds to load models)...")
lyra_ai = MiniAI()
print("Lyra AI initialized and ready.")

viewer_tracker = ViewerTracker()
chat_analyzer = ChatPatternAnalyzer()
yt_poller = YouTubeChatPoller(viewer_tracker=viewer_tracker)

# Start VTube Studio Bridge
vts_bridge.start()

# Lock để đảm bảo chỉ có 1 thread được gọi lyra_ai.chat() tại một thời điểm
ai_chat_lock = threading.Lock()

# ========================
# AUDIO QUEUE FOR TTS
# ========================
import queue as _queue
audio_play_queue = _queue.Queue()

def _audio_worker():
    """Luồng xử lý phát audio tuần tự, tránh chồng chéo âm thanh"""
    while True:
        try:
            # Lấy data từ queue (block cho đến khi có)
            audio_data, frame_rate, device_id = audio_play_queue.get()
            
            # Chuyển đổi sang numpy array và phát
            samples = np.array(audio_data)
            sd.play(samples, samplerate=frame_rate, device=device_id)
            
            # Chờ cho đến khi âm thanh phát xong trước khi lấy câu tiếp theo
            sd.wait()
            
            audio_play_queue.task_done()
        except Exception as e:
            print(f"[AudioWorker] Error: {e}")
            time.sleep(0.1)

# Khởi chạy luồng audio worker
audio_thread = threading.Thread(target=_audio_worker, daemon=True)
audio_thread.start()

def clear_audio_queue():
    """Xóa tất cả các âm thanh đang chờ phát và dừng âm thanh hiện tại (Action Interruption)."""
    while not audio_play_queue.empty():
        try:
            audio_play_queue.get_nowait()
            audio_play_queue.task_done()
        except _queue.Empty:
            break
    try:
        sd.stop()
    except Exception as e:
        print(f"[AudioWorker] Lỗi khi dừng âm thanh: {e}")

# ========================
# Proactive Chat Monitor
# ========================


def _proactive_monitor():
    """Background thread: if stream active and chat silent >2 min, Lyra asks a question."""
    while True:
        time.sleep(30)
        try:
            if not lyra_ai.is_streaming:
                continue
            last_time = getattr(lyra_ai, "_last_viewer_message_time", None)
            if last_time is None:
                continue
            gap = (get_now_vn() - last_time).total_seconds()
            if gap > 120:
                # Không trigger nếu audio đang phát — tránh ngắt giữa câu
                if not audio_play_queue.empty():
                    continue
                prompt = (
                    "Chat đã im lặng 2 phút. Đặt một câu hỏi ngắn, tò mò để khơi gợi mọi người tâm sự "
                    "để giữ người xem ở lại."
                )
                with ai_chat_lock:
                    question = lyra_ai._call_light_model(
                        messages=[
                            {
                                "role": "system",
                                "content": "Bạn là Lyra, 16 tuổi, hỏi thăm ngắn gọn, tự nhiên, tò mò.",
                            },
                            {"role": "user", "content": prompt},
                        ],
                        temperature=0.4,
                        max_tokens=60,
                    )
                if question:
                    question = question.strip()
                    _sse_broadcast(
                        {
                            "type": "proactive_question",
                            "reply": question,
                            "emotion": "thinking",
                            "action": "THINK",
                            "sender_name": "Lyra",
                            "source_type": "system",
                        }
                    )
                    # Reset timer to avoid spam
                    lyra_ai._last_viewer_message_time = get_now_vn()
        except Exception as e:
            print(f"[ProactiveMonitor] {e}")


# ========================
# ROUTES
# ========================


@app.route("/")
def index():
    session.permanent = True
    app.permanent_session_lifetime = timedelta(days=365)
    return render_template("index.html")


@app.route("/reset")
@limiter.limit("10 per minute")
@require_auth
def reset():
    """Xóa session nhưng GIỮ memory.db"""
    global lyra_ai
    session.clear()
    lyra_ai = MiniAI()  # Reload AI base state
    return "Session cleared (memory.db preserved)"


@app.route("/reset-all")
@limiter.limit("3 per minute")
@require_auth
def reset_all():
    """Xóa toàn bộ session + memory.db"""
    global lyra_ai
    session.clear()
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    lyra_ai = MiniAI()  # Initialize fresh AI memory
    return "All cleared (session + memory)"


@limiter.limit("30 per minute")
@app.route("/chat", methods=["POST"])
def chat():

    try:
        session.permanent = True

        data = request.get_json()

        print("Incoming request:", data)

        if not data or "message" not in data:
            return jsonify({"error": "Invalid request"}), 400

        user_input = sanitize_input(data["message"], max_length=1000)

        if user_input == "":
            return jsonify({"reply": "Please say something."})

        # ===== GENERATE AI REPLY =====
        # Ngắt tiếng đang nói nếu người dùng chat đè (Action Interruption)
        clear_audio_queue()
        
        # Kích hoạt biểu cảm Thinking trước khi AI xử lý
        vts_bridge.trigger_emotion("thinking")

        # Owner chat qua web — source_type = "owner", full memory
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

        response_payload = build_state_payload(lyra_ai, result=result)

        # VTube Studio triggering is now handled by the Orchestrator in lyra_ai.chat()
        # including emotions, actions, and VAD parameters.

        return jsonify(response_payload)

    except Exception:
        print("ERROR OCCURRED")
        traceback.print_exc()

        return jsonify(
            {
                "reply": "Something went wrong...",
                "emotion": "neutral",
                "affection": 50,
                "mood": 0,
                "time_period": "afternoon",
                "time_gap_hours": None,
            }
        )


#
# ========================
# TTS ROUTE
# ========================


def apply_pitch_shift(audio_bytes, octaves=0.22):
    """
    Tăng pitch cho audio bằng cách thay đổi sample rate.
    Điều này sẽ làm audio nhanh hơn một chút, phù hợp với giọng em gái tinh nghịch.
    """
    try:
        # Load audio từ bytes
        audio = AudioSegment.from_file(io.BytesIO(audio_bytes), format="mp3")
        
        # Tính toán sample rate mới dựa trên số octaves
        # 0.22 - 0.25 octaves là mức phù hợp cho Lyra 16 tuổi
        new_sample_rate = int(audio.frame_rate * (2.0 ** octaves))
        
        # Override frame rate để đổi pitch (và speed)
        shifted_audio = audio._spawn(audio.raw_data, overrides={'frame_rate': new_sample_rate})
        
        # Thiết lập lại frame rate về chuẩn để player hiểu đúng
        shifted_audio = shifted_audio.set_frame_rate(audio.frame_rate)
        
        # Xuất ra bytes
        out_io = io.BytesIO()
        shifted_audio.export(out_io, format="mp3")
        return out_io.getvalue()
    except Exception as e:
        print(f"[PitchShift] Error: {e}")
        return audio_bytes


def play_to_cable(audio_bytes, device_id=VB_CABLE_DEVICE_ID):
    """
    Phát audio trực tiếp ra thiết bị VB Cable (ID 15) để OBS bắt được.
    """
    try:
        # Load audio từ bytes
        audio = AudioSegment.from_file(io.BytesIO(audio_bytes), format="mp3")
        
        # Chuyển đổi AudioSegment sang numpy array
        samples = np.array(audio.get_array_of_samples())
        
        # Xử lý nếu là Stereo
        if audio.channels == 2:
            samples = samples.reshape((-1, 2))
            
        # Chuẩn hóa về float32 [-1.0, 1.0] để sounddevice phát chuẩn
        samples = samples.astype(np.float32) / (2**15)
        
        # Đẩy vào queue để phát tuần tự thay vì sd.play trực tiếp
        audio_play_queue.put((samples.tolist(), audio.frame_rate, device_id))
        
    except Exception as e:
        print(f"[AudioPlayback] Lỗi khi phát ra VB Cable: {e}")


@limiter.limit("20 per minute")
@app.route("/speak", methods=["POST"])
def speak():
    """FPT AI TTS — trả về audio mp3"""
    try:
        data = request.get_json()
        if not data or "text" not in data:
            return jsonify({"error": "No text provided"}), 400

        text = data["text"].strip()
        if not text:
            return jsonify({"error": "Empty text"}), 400
        print(f"[TTS] Request text: {text[:120]!r}")

        # ── Prosody Speed Mapping (Paralinguistics — Module 5) ───────────────
        # Map attention (arousal proxy) → FPT speed string
        # FPT speed: "-3" (rất chậm) → "0" (bình thường) → "3" (rất nhanh)
        # attention: 0 → 10 (từ EmotionEngine)
        attention = lyra_ai.emotion.attention
        if attention <= 2:
            tts_speed = "-1"  # Lyra mệt → nói chậm, kéo dài
        elif attention <= 4:
            tts_speed = "0"  # Hơi mệt → hơi chậm
        elif attention >= 8:
            tts_speed = "2"  # Hào hứng → nói nhanh hơn
        else:
            tts_speed = "1"  # Bình thường

        response = requests.post(
            FPT_TTS_URL,
            data=text.encode("utf-8"),
            headers={
                "api-key": FPT_API_KEY,
                "voice": FPT_TTS_VOICE,
                "speed": tts_speed,
                "Content-Type": "application/octet-stream",
            },
            timeout=15,
        )

        if response.status_code != 200:
            print(f"[TTS] FPT error: {response.status_code} - {response.text}")
            return jsonify({"error": "TTS failed", "detail": response.text}), 500

        # FPT trả về JSON có field "async" chứa URL mp3
        result = response.json()
        audio_url = result.get("async")
        if not audio_url:
            return jsonify({"error": "No audio URL returned", "detail": result}), 500

        # FPT xử lý TTS async — Chờ và thử lại tối đa 4 lần (mỗi lần 1s)
        import time as _t

        audio_res = None
        # Polling: FPT cần thời gian để xử lý (thường 2-10s).
        # Tăng số lần thử lên 10 lần, mỗi lần cách nhau 1.5s.
        for attempt in range(10):
            _t.sleep(1.5)
            try:
                temp_res = requests.get(audio_url, timeout=5)
                if temp_res.status_code == 200 and temp_res.headers.get('Content-Type', '').startswith('audio/'):
                    audio_res = temp_res
                    break
                # 404 có nghĩa là file đang được xử lý, chưa sẵn sàng.
                if temp_res.status_code != 404:
                    print(f"[TTS] Polling status: {temp_res.status_code}")
                
                if attempt == 9: # Lần cuối cùng
                    print(f"[TTS] Timeout: Không lấy được file audio sau 10 lần thử.")
            except Exception as _e:
                print(f"[TTS] Lấy audio lỗi: {_e}")

        if not audio_res:
            print(f"[TTS] Audio URL lỗi hoặc bị FPT delay quá lâu!")
            return jsonify({"error": "Audio fetch failed after 4s"}), 500

        # ── Áp dụng Pitch Shifting để giọng trẻ con hơn ───────────────────────
        try:
            final_audio = apply_pitch_shift(audio_res.content, octaves=0.22)
            # Phát ra VB Cable
            play_to_cable(final_audio, device_id=VB_CABLE_DEVICE_ID)
        except Exception:
            final_audio = audio_res.content

        return jsonify({"ok": True, "audio_output": "vb_cable", "device_id": VB_CABLE_DEVICE_ID})

    except Exception:
        print("[TTS] ERROR")
        traceback.print_exc()
        return jsonify({"error": "TTS internal error"}), 500


@app.route("/analytics")
def get_analytics():
    """Get analytics data from DB"""
    try:
        if not os.path.exists(DB_PATH):
            return jsonify(
                {
                    "emotions": {},
                    "moodHistory": [],
                    "totalMessages": 0,
                    "conversationCount": 0,
                    "userName": "Not Set",
                    "favoriteTopics": [],
                }
            )

        with DB_LOCK:
            conn = get_db()
            c = conn.cursor()
            name_row = c.execute("SELECT value FROM profile WHERE key='name'").fetchone()
            total_row = c.execute(
                "SELECT value FROM metadata WHERE key='total_messages'"
            ).fetchone()
            topics = [
                r[0]
                for r in c.execute(
                    "SELECT value FROM facts WHERE type='topic' ORDER BY id DESC LIMIT 10"
                )
            ]
            conn.close()

        return jsonify(
            {
                "emotions": {},
                "moodHistory": [],
                "totalMessages": int(total_row[0]) if total_row else 0,
                "conversationCount": 0,
                "userName": name_row[0] if name_row else "Not Set",
                "favoriteTopics": topics,
            }
        )
    except Exception as e:
        print(f"Error getting analytics: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/status")
def status():
    return jsonify(build_state_payload(lyra_ai))


@app.route("/session-info")
def session_info():
    """Debug: Xem thông tin internal AI state"""
    return jsonify(
        {
            "message_count": len(lyra_ai.messages),
            "mood": lyra_ai.mood,
            "affection": lyra_ai.affection,
            "attention": lyra_ai.attention,
        }
    )


@app.route("/favicon.ico")
def favicon():
    return "", 204


@app.route("/history", methods=["GET"])
def get_history():
    """Trả về lịch sử chat từ DB"""
    try:
        if not os.path.exists(DB_PATH):
            return jsonify({"history": []})
        with DB_LOCK:
            conn = get_db()
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


# ========================
# STREAM CHAT ROUTE
# ========================


def _trigger_stream_summary(channel_id: str, platform: str):
    """
    Giai đoạn 4: Tạo summary định kỳ cho chat stream.
    Gọi AI tóm tắt "chat đang nói về gì" → lưu vào episodic memory của Lyra + stream_summaries DB.
    Hàm này được chạy trong background worker (non-blocking).
    """
    try:
        recent = chat_analyzer.get_recent_summaries(channel_id, platform, limit=1)
        prev_summary = recent[0]["summary"] if recent else ""

        # Lấy top words hiện tại để làm input cho AI
        style = chat_analyzer.get_style_hints(channel_id, platform)
        top_viewers = viewer_tracker.get_top_viewers(
            platform=platform, channel_id=channel_id, limit=5
        )
        top_names = (
            ", ".join(v["viewer_name"] for v in top_viewers)
            if top_viewers
            else "chưa có"
        )

        prompt_content = (
            f"Đây là thông tin về buổi livestream:\n"
            f"- Top chatters: {top_names}\n"
            f"{style}\n"
        )
        if prev_summary:
            prompt_content += f"- Summary trước: {prev_summary}\n"

        prompt_content += (
            "\nTóm tắt ngắn (1-2 câu) chat đang nói về gì và vibe của kênh lúc này."
        )

        with ai_chat_lock:
            summary = lyra_ai._call_light_model(
                [
                    {
                        "role": "system",
                        "content": "Bạn là assistant tóm tắt livestream chat. Trả lời bằng tiếng Việt, ngắn gọn.",
                    },
                    {"role": "user", "content": prompt_content},
                ],
                temperature=0.3,
                max_tokens=80,
            )

        if summary:
            summary = summary.strip()
            # Lưu vào DB stream_summaries
            chat_analyzer.save_stream_summary(summary, channel_id, platform)
            # Shared-memory mode: stream summary cũng đi vào long-term episodic memory.
            lyra_ai.memory.add_item(
                "episodic", f"[Stream] {summary}", weight=1.1, limit=12
            )
            # Đồng thời cache tạm trong session để tăng phản xạ theo buổi stream hiện tại.
            try:
                lyra_ai.memory.add_session_item(
                    f"[Stream vibe] {summary[:160]}", kind="session"
                )
            except Exception:
                pass
            print(f"[Stream] Summary: {summary}")

    except Exception as e:
        print(f"[Stream] Summary error: {e}")


@limiter.limit("60 per minute")
@app.route("/stream-chat", methods=["POST"])
def stream_chat():
    """
    Endpoint cho livestream chat.

    Input JSON (chat_event):
    {
        "message": "hello Lyra!",
        "sender_id": "UCxxxx",           // required - định danh chính
        "sender_name": "Viewer123",    // optional - tên hiển thị
        "channel_id": "main_stream",  // optional - ID kênh chat
        "platform": "youtube",           // optional - youtube/twitch/discord
        "role": "viewer"               // optional - viewer/chat/stream_chat
    }

    Response JSON:
    {
        "reply": "...", "emotion": "...", "action": "NONE",
        "mood": 0, "affection": 50, "time_period": "...",
        "sender_id": "...", "sender_name": "...",
        "channel_id": "...", "platform": "...", "role": "..."
    }
    """
    try:
        data = request.get_json()

        if not data or "message" not in data or "sender_id" not in data:
            return jsonify(
                {"error": "Missing required fields: message, sender_id"}
            ), 400

        message = sanitize_input(data["message"], max_length=1000)
        sender_id = str(data["sender_id"]).strip()
        sender_name = str(data.get("sender_name", "Viewer")).strip()
        channel_id = str(data.get("channel_id", "default")).strip()
        platform = str(data.get("platform", "unknown")).strip()
        role = str(data.get("role", "viewer")).strip()  # viewer | chat | stream_chat

        if not message:
            return jsonify({"error": "Empty message"}), 400

        if not sender_id:
            return jsonify({"error": "Empty sender_id"}), 400

        # Inject sender context vào message để Lyra biết ai đang nói
        # Format: "[TênViewer]: nội dung" — đơn giản, tự nhiên
        composed_input = f"[{sender_name}]: {message}"

        print(
            f"[Stream] {platform}/{channel_id} | {sender_name} ({sender_id}) [{role}]: {message}"
        )

        # Giai đoạn 2 & 3: Track viewer + build stream context
        viewer_info = viewer_tracker.record_message(
            sender_id, sender_name, platform, channel_id, message
        )

        # Giai đoạn 4: Thu thập chat pattern
        chat_analyzer.ingest(message, channel_id, platform, sender_id=sender_id)
        lyra_ai.rl_loop.ingest_viewer_message(message, sender_name)
        style_hints = chat_analyzer.get_style_hints(channel_id, platform)

        # Inject stream context + style hints vào Lyra trước khi gọi chat()
        stream_ctx = viewer_tracker.get_stream_context(
            sender_id, sender_name, platform, channel_id, viewer_info
        )
        if style_hints:
            stream_ctx = f"{stream_ctx}\n{style_hints}" if stream_ctx else style_hints
        content_ctx = _build_stream_content_context()
        if content_ctx:
            stream_ctx = f"{content_ctx}\n{stream_ctx}" if stream_ctx else content_ctx
        # Giữ nguyên việc inject stream_ctx
        lyra_ai.stream_context = stream_ctx

        if not chat_analyzer.should_extract_memory(viewer_info):
            lyra_ai._thread_local.skip_memory_extraction = True

        # Xác định source_type và viewer_data
        regular = viewer_tracker.is_regular_viewer(sender_id, platform)
        is_donor = data.get("is_donor", False)
        if is_donor:
            source_type_val = "donor"
            amount = data.get("donate_amount", "")
            viewer_data = {
                "viewer_name": sender_name,
                "affection": regular["affection"] if regular else 40,
                "amount": amount,
                "gender": data.get("gender", "male"),
            }
            record_donation(sender_name, amount)
        elif regular:
            source_type_val = "regular_viewer"
            viewer_data = {
                "viewer_name": sender_name,
                "affection": regular["affection"],
                "total_streams": regular["total_streams"],
                "gender": data.get("gender", "male"),
            }
            if viewer_info.get("message_count", 1) == 1:
                record_regular_arrival(sender_name, regular["total_streams"], regular["affection"])
        else:
            source_type_val = "new_viewer"
            viewer_data = {
                "viewer_name": sender_name,
                "gender": data.get("gender", "male"),
            }

        # Kích hoạt biểu cảm Thinking
        vts_bridge.trigger_emotion("thinking")

        with ai_chat_lock:
            result = lyra_ai.chat(
                composed_input,
                source_type=source_type_val,
                viewer_data=viewer_data,
                stream_context=stream_ctx,
            )

        # Giai đoạn 4: Stream summary định kỳ
        if chat_analyzer.should_summarize():
            enqueue(PRIORITY_HIGH, _trigger_stream_summary, channel_id, platform)

        response_payload = build_state_payload(lyra_ai, result=result)
        response_payload.update(
            {
                "sender_id": sender_id,
                "sender_name": sender_name,
                "channel_id": channel_id,
                "platform": platform,
                "role": role,
                "source_type": source_type_val,
                "viewer_message_count": viewer_info.get("message_count", 1),
                "viewer_affinity": viewer_info.get("affinity_score", 1.0),
            }
        )

        return jsonify(response_payload)

    except Exception:
        print("STREAM CHAT ERROR")
        traceback.print_exc()
        return jsonify(
            {
                "error": "Internal server error",
                "reply": "...",
                "emotion": "neutral",
                "affection": 50,
                "mood": 0,
            }
        ), 500


@app.route("/viewers", methods=["GET"])
def get_viewers():
    """
    Trả về top viewers theo message_count.
    Query params: platform, channel_id, limit (default 10)
    """
    platform = request.args.get("platform")
    channel_id = request.args.get("channel_id")
    limit = min(int(request.args.get("limit", 10)), 50)

    top = viewer_tracker.get_top_viewers(
        platform=platform, channel_id=channel_id, limit=limit
    )
    return jsonify({"viewers": top, "count": len(top)})


@app.route("/proactive", methods=["GET"])
def proactive():
    """Lyra chủ động nhắn khi user vắng lâu"""
    try:
        with ai_chat_lock:
            msg = lyra_ai.get_proactive_message()

        if not msg:
            return jsonify({"message": None, "should_show": False})

        # Cập nhật last_message_time để không spam
        lyra_ai.memory["time_tracking"]["last_message_time"] = datetime.now(
            pytz.timezone("Asia/Ho_Chi_Minh")
        ).isoformat()
        lyra_ai.memory._is_dirty = True
        lyra_ai.save_memory()

        response_payload = build_state_payload(lyra_ai)
        response_payload.update(
            {
                "message": msg,
                "should_show": True,
            }
        )
        return jsonify(response_payload)

    except Exception:
        traceback.print_exc()
        return jsonify({"message": None, "should_show": False})


# ========================
# YOUTUBE STREAM CONTROL
# ========================

import queue as _queue
import time as _time

# Priority Queue cho stream events
# Tier 0: owner (STT từ web) — xử lý ngay, bypass queue
# Tier 1: donor
# Tier 2: regular_viewer
# Tier 3: new_viewer (random pick)
_priority_queues: dict = {
    "owner": _queue.Queue(maxsize=10),
    "donor": _queue.Queue(maxsize=20),
    "regular_viewer": _queue.Queue(maxsize=50),
    "new_viewer": _queue.Queue(maxsize=200),
}
_new_viewer_pool: list = []  # pool để random pick
_pool_lock = threading.Lock()

# Track regular viewers đã được chào trong session này — tránh chào lại
_greeted_viewers_this_session: set = set()
_greeted_lock = threading.Lock()

# Cooldown giữa các TTS response (giây)
REPLY_COOLDOWN = STREAM_REPLY_COOLDOWN
_last_reply_time: float = 0.0
_reply_lock = threading.Lock()

# Random pick interval cho new_viewer (giây)
NEW_VIEWER_PICK_INTERVAL = STREAM_NEW_VIEWER_INTERVAL
_last_new_viewer_pick: float = 0.0


def _can_reply() -> bool:
    """True nếu đã qua cooldown"""
    return (_time.time() - _last_reply_time) >= REPLY_COOLDOWN


def _mark_replied():
    global _last_reply_time
    with _reply_lock:
        _last_reply_time = _time.time()


def _enqueue_stream_event(chat_event: dict):
    """
    Phân loại và đẩy event vào đúng queue theo priority.
    Owner (source_type='owner') không đi qua queue — xử lý ngay ở /chat.
    """
    sender_id = chat_event.get("sender_id", "")
    platform = chat_event.get("platform", "youtube")
    is_donor = chat_event.get("is_donor", False)

    regular = viewer_tracker.is_regular_viewer(sender_id, platform)
    is_owner = chat_event.get("is_owner", False)

    if is_owner:
        tier = "owner"
    elif is_donor:
        tier = "donor"
    elif regular:
        tier = "regular_viewer"
        chat_event["_regular_data"] = dict(regular)
    else:
        tier = "new_viewer"

    chat_event["_tier"] = tier

    if tier == "new_viewer":
        with _pool_lock:
            # Dedup: mỗi sender_id chỉ giữ 1 slot (tin nhắn mới nhất)
            _new_viewer_pool[:] = [
                e
                for e in _new_viewer_pool
                if e.get("sender_id") != chat_event.get("sender_id")
            ]
            _new_viewer_pool.append(chat_event)
            # Giữ pool tối đa 100 để tránh RAM bloat
            if len(_new_viewer_pool) > 100:
                _new_viewer_pool.pop(0)
    else:
        try:
            _priority_queues[tier].put_nowait(chat_event)
        except _queue.Full:
            print(
                f"[Queue] {tier} queue full, dropping message from {chat_event.get('sender_name')}"
            )


def _process_queue_loop():
    """
    Priority consumer loop — chạy trong background thread.
    Lấy từ yt_poller → phân loại vào priority queues → xử lý theo thứ tự:
    donor → regular_viewer → random new_viewer
    Tuân thủ REPLY_COOLDOWN giữa các response.
    """
    global _last_new_viewer_pick

    while True:
        try:
            if not yt_poller._is_running:
                _time.sleep(1)
                continue

            # Drain yt_poller queue vào priority queues của mình
            while True:
                raw = yt_poller.get_next_message(timeout=0.05)
                if raw is None:
                    break
                _enqueue_stream_event(raw)

            # ── Consensus: check pending exclamation → inject vào donor queue ──
            # Chạy sau drain để có đủ messages mới nhất trước khi check
            consensus_event = chat_analyzer.get_pending_consensus_exclamation()
            if consensus_event is not None:
                synthetic = {
                    "message": consensus_event.hint,
                    "sender_id": "__consensus__",
                    "sender_name": "Chat",
                    "_tier": "donor",
                    "_is_consensus": True,
                    "_consensus_type": consensus_event.type,
                }
                try:
                    _priority_queues["donor"].put_nowait(synthetic)
                    print(
                        f"[Consensus] Synthetic event queued: {consensus_event.type} "
                        f"({consensus_event.unique_count}/{consensus_event.total_unique} = "
                        f"{consensus_event.percent:.0%})"
                    )
                except _queue.Full:
                    pass  # donor queue full, skip

            has_owner_waiting = not _priority_queues["owner"].empty()
            if not has_owner_waiting and not _can_reply():
                _time.sleep(0.3)
                continue

            event = None

            # Tier 0: owner
            try:
                event = _priority_queues["owner"].get_nowait()
            except _queue.Empty:
                pass

            # Tier 1: donor
            if event is None:
                try:
                    event = _priority_queues["donor"].get_nowait()
                except _queue.Empty:
                    pass

            # Tier 2: regular_viewer
            if event is None:
                try:
                    event = _priority_queues["regular_viewer"].get_nowait()
                except _queue.Empty:
                    pass

            # Tier 3: random new_viewer (mỗi NEW_VIEWER_PICK_INTERVAL giây)
            if event is None:
                now = _time.time()
                if (now - _last_new_viewer_pick) >= NEW_VIEWER_PICK_INTERVAL:
                    with _pool_lock:
                        pool_copy = list(_new_viewer_pool)  # snapshot để tránh race condition
                        if pool_copy:
                            import random as _random
                            event = _random.choice(pool_copy)
                            # Chỉ xóa nếu vẫn còn trong pool (tránh KeyError nếu bị clear đồng thời)
                            if event in _new_viewer_pool:
                                _new_viewer_pool.remove(event)
                    _last_new_viewer_pick = now

            if event is None:
                _time.sleep(0.3)
                continue

            _handle_stream_event(event)
            # Owner bypasses cooldown — chỉ set cooldown cho non-owner tiers
            if event.get("_tier") != "owner":
                _mark_replied()

        except Exception as e:
            print(f"[Stream Consumer] Error: {e}")
            _time.sleep(1)


def _handle_stream_event(chat_event: dict):
    """
    Xử lý 1 chat event từ YouTube — tái dùng toàn bộ logic của stream_chat.
    Kết quả được broadcast qua SSE tới frontend.
    """
    try:
        message = chat_event["message"]
        sender_id = chat_event["sender_id"]
        sender_name = chat_event["sender_name"]
        platform = chat_event.get("platform", "youtube")
        channel_id = chat_event.get("channel_id", "default")
        is_consensus = chat_event.get("_is_consensus", False)

        tier = chat_event.get("_tier", "new_viewer")

        # ── Synthetic consensus event: không record vào viewer_stats ──────────
        if is_consensus:
            # Lyra nhận hint trực tiếp, không cần viewer context
            source_type_val = "new_viewer"
            viewer_data = {"viewer_name": "Chat"}
            stream_ctx = _build_stream_content_context()
            # Thêm velocity hint nếu có
            velocity_hint = chat_analyzer.get_velocity_hint()
            if velocity_hint:
                stream_ctx = (
                    f"{stream_ctx}\n{velocity_hint}" if stream_ctx else velocity_hint
                )
            composed_input = message  # consensus uses raw message
            lyra_ai._last_viewer_message_time = get_now_vn()
            with ai_chat_lock:
                result = lyra_ai.chat(
                    composed_input,
                    source_type=source_type_val,
                    viewer_data=viewer_data,
                    stream_context=stream_ctx,
                )
            payload = build_state_payload(lyra_ai, result=result)
            payload.update(
                {
                    "sender_id": "__consensus__",
                    "sender_name": "Chat",
                    "source_type": "consensus",
                    "is_consensus": True,
                }
            )
            if result:
                if result.get("emotion"):
                    vts_bridge.trigger_emotion(result["emotion"])
                if result.get("action"):
                    vts_bridge.trigger_action(result["action"])
                if result.get("vad"):
                    v, a, d = result["vad"]
                    vts_bridge.update_vad_params(v, a, d)
            _sse_broadcast(payload)
            return

        if tier == "owner":
            composed_input = message
        else:
            composed_input = f"[{sender_name}]: {message}"

        print(f"[Stream Consumer] [{tier}] {sender_name}: {message}")

        viewer_info = viewer_tracker.record_message(
            sender_id, sender_name, platform, channel_id, message
        )

        chat_analyzer.ingest(message, channel_id, platform, sender_id=sender_id)
        style_hints = chat_analyzer.get_style_hints(channel_id, platform)

        stream_ctx = viewer_tracker.get_stream_context(
            sender_id, sender_name, platform, channel_id, viewer_info
        )
        if style_hints:
            stream_ctx = f"{stream_ctx}\n{style_hints}" if stream_ctx else style_hints
        content_ctx = _build_stream_content_context()
        if content_ctx:
            stream_ctx = f"{content_ctx}\n{stream_ctx}" if stream_ctx else content_ctx

        # ── Inject discussion hint + velocity hint vào mọi viewer message ─────
        discussion_hint = chat_analyzer.get_active_discussion_hint()
        if discussion_hint:
            stream_ctx = (
                f"{stream_ctx}\n{discussion_hint}" if stream_ctx else discussion_hint
            )
        velocity_hint = chat_analyzer.get_velocity_hint()
        if velocity_hint:
            stream_ctx = (
                f"{stream_ctx}\n{velocity_hint}" if stream_ctx else velocity_hint
            )

        # ── IDEA-01: Regular viewer arrival hint ──────────────────────────────
        # Nếu đây là tin đầu tiên của regular viewer trong session → inject hint chào
        if tier == "regular_viewer" and viewer_info.get("message_count", 0) == 1:
            with _greeted_lock:
                already_greeted = sender_id in _greeted_viewers_this_session
                if not already_greeted:
                    _greeted_viewers_this_session.add(sender_id)

            if not already_greeted:
                from prompts import REGULAR_VIEWER_ARRIVAL_HINT

                regular_data_local = chat_event.get("_regular_data") or {}
                arrival_hint = REGULAR_VIEWER_ARRIVAL_HINT.format(
                    viewer_name=sender_name,
                    total_streams=regular_data_local.get("total_streams", 1),
                    affection=regular_data_local.get("affection", 35),
                )
                stream_ctx = (
                    f"{stream_ctx}\n{arrival_hint}" if stream_ctx else arrival_hint
                )
                # ── Live Context: note regular return ─────────────────────────
                record_regular_arrival(
                    viewer_name=sender_name,
                    total_streams=regular_data_local.get("total_streams", 1),
                    affection=regular_data_local.get("affection", 35),
                )

        if not chat_analyzer.should_extract_memory(viewer_info):
            lyra_ai._thread_local.skip_memory_extraction = True

        regular_data = chat_event.get("_regular_data")
        # gender từ chat_event (YouTube API không cung cấp — default male)
        _gender = chat_event.get("gender", "male")

        if tier == "owner":
            source_type_val = "owner"
            viewer_data = None
        elif tier == "donor":
            source_type_val = "donor"
            viewer_data = {
                "viewer_name": sender_name,
                "affection": regular_data["affection"] if regular_data else 40,
                "amount": chat_event.get("donate_amount", ""),
                "gender": _gender,
            }
        elif tier == "regular_viewer":
            source_type_val = "regular_viewer"
            viewer_data = {
                "viewer_name": sender_name,
                "affection": regular_data["affection"] if regular_data else 35,
                "total_streams": regular_data["total_streams"] if regular_data else 1,
                "gender": _gender,
            }
        else:
            source_type_val = "new_viewer"
            viewer_data = {"viewer_name": sender_name, "gender": _gender}

        # ── Live Context: donations ─────────────────────────────────────
        if tier == "donor":
            amount = chat_event.get("donate_amount", "")
            record_donation(viewer_name=sender_name, amount=amount)

        # Update last activity timestamp for proactive monitoring
        lyra_ai._last_viewer_message_time = get_now_vn()

        with ai_chat_lock:
            result = lyra_ai.chat(
                composed_input,
                source_type=source_type_val,
                viewer_data=viewer_data,
                stream_context=stream_ctx,
            )

        if chat_analyzer.should_summarize():
            enqueue(PRIORITY_HIGH, _trigger_stream_summary, channel_id, platform)

        payload = build_state_payload(lyra_ai, result=result)
        payload.update(
            {
                "sender_id": sender_id,
                "sender_name": sender_name,
                "channel_id": channel_id,
                "platform": platform,
                "source_type": source_type_val,
                "viewer_message_count": viewer_info.get("message_count", 1),
                "viewer_affinity": viewer_info.get("affinity_score", 1.0),
            }
        )

        # Sync with VTube Studio
        if result:
            if result.get("emotion"):
                vts_bridge.trigger_emotion(result["emotion"])
            if result.get("action"):
                vts_bridge.trigger_action(result["action"])
            # ── VAD → Live2D params (Paralinguistics — Module 5) ─────────────
            if result.get("vad"):
                v, a, d = result["vad"]
                vts_bridge.update_vad_params(v, a, d)

        _sse_broadcast(payload)

    except Exception as e:
        print(f"[Stream Consumer] handle error: {e}")
        import traceback as tb

        tb.print_exc()


# SSE broadcast — gửi event tới tất cả client đang subscribe /stream/events
import json as _json

_sse_subscribers: list = []
_sse_lock = threading.Lock()
MAX_SSE_SUBSCRIBERS = 10  # Giới hạn để tránh memory leak khi nhiều client bị drop đột ngột


def _sse_broadcast(data: dict):
    """Push data tới tất cả SSE subscribers"""
    msg = f"data: {_json.dumps(data, ensure_ascii=False)}\n\n"
    with _sse_lock:
        dead = []
        for q in _sse_subscribers:
            try:
                q.put_nowait(msg)
            except Exception:
                dead.append(q)
        for q in dead:
            _sse_subscribers.remove(q)


# Khởi động consumer loop khi server start
_consumer_thread = threading.Thread(target=_process_queue_loop, daemon=True)
_consumer_thread.start()


@app.route("/stream/content", methods=["GET"])
def stream_get_content():
    """Trả về stream content hiện tại (từ config.py)"""
    return jsonify(
        {
            "title": STREAM_TITLE,
            "game": STREAM_GAME,
            "goals": STREAM_GOALS,
            "notes": STREAM_NOTES,
            "context_string": _build_stream_content_context(),
        }
    )


def _send_farewell_async(channel_id: str, platform: str) -> None:
    """Generate và broadcast lời tạm biệt cuối stream. Module-level function — không cần closure."""
    try:
        recent_summaries = chat_analyzer.get_recent_summaries(channel_id, platform, limit=1)
        summary_text = recent_summaries[0]["summary"] if recent_summaries else ""
        top_viewers_list = viewer_tracker.get_top_viewers(
            platform=platform, channel_id=channel_id, limit=3
        )
        top_names = (
            ", ".join(v["viewer_name"] for v in top_viewers_list)
            if top_viewers_list
            else "mọi người"
        )

        with ai_chat_lock:
            farewell = lyra_ai.generate_stream_event_reply(
                "farewell",
                {
                    "summary": summary_text,
                    "top_viewers": top_names,
                    "duration": "",
                },
                temperature=0.1,
            )
        if farewell:
            _sse_broadcast(
                {
                    "type": "stream_event",
                    "event": "stream_stop",
                    "reply": farewell,
                    "emotion": "friendly",
                    "action": "WAVE",
                    "sender_name": "Lyra",
                    "source_type": "system",
                }
            )
            print(f"[Stream] Farewell: {farewell}")
    except Exception as e:
        print(f"[Stream] Farewell error: {e}")


@app.route("/stream/start", methods=["POST"])
def stream_start():
    """
    Bắt đầu poll YouTube Live Chat.
    Body JSON: { "live_chat_id": "...", "video_id": "..." (optional) }
    Dùng credentials từ session (phải authorize trước).
    """
    try:
        credentials = session.get("credentials") or _load_youtube_credentials()
        if not credentials:
            return jsonify({
                "error": "Not authorized. Visit /authorize first.",
                "authorize_url": url_for("authorize"),
            }), 401
        session["credentials"] = credentials

        data = request.get_json() or {}
        live_chat_id = (data.get("live_chat_id") or DEFAULT_YOUTUBE_LIVE_CHAT_ID).strip()
        video_id = (data.get("video_id") or DEFAULT_YOUTUBE_VIDEO_ID).strip()

        # Nếu không có live_chat_id, thử lấy từ video_id
        if not live_chat_id and video_id:
            live_chat_id = get_live_chat_id(credentials, video_id)
            if not live_chat_id:
                return jsonify(
                    {"error": f"Could not find live chat for video_id={video_id}"}
                ), 404
        
        # Nếu vẫn không có live_chat_id, thử tìm stream đang active tự động
        if not live_chat_id:
            print("[YouTube] Đang tự động tìm buổi stream đang active...")
            auto_video_id, auto_live_chat_id = get_current_live_stream_info(credentials)
            if auto_live_chat_id:
                video_id = auto_video_id
                live_chat_id = auto_live_chat_id

        if not live_chat_id:
            return jsonify({"error": "Provide live_chat_id or video_id"}), 400

        # ── WARM-UP: Clear old session data & prime AI ──────────────
        platform = data.get("platform", "youtube")
        channel_id = data.get("channel_id", "default")

        viewer_tracker.clear_session_stats(platform, channel_id)
        chat_analyzer.reset_session_patterns(channel_id, platform)
        with ai_chat_lock:
            lyra_ai.prepare_for_stream()
        enqueue(PRIORITY_NORMAL, lyra_ai._generate_stream_plan)

        # ── Live Context: mark stream active with focus ─────────────────────
        focus = STREAM_TITLE or STREAM_GAME or "streaming"
        set_stream_active(True, focus=focus)

        # ── Constraints from stream notes ─────────────────────────────────
        notes = (STREAM_NOTES or "").lower()
        if "no spoil" in notes or "không spoil" in notes:
            add_constraint("no spoilers")

        result = yt_poller.start(credentials, live_chat_id)

        # ── IDEA-02: Stream greeting ──────────────────────────
        # Generate câu chào mở màn và broadcast qua SSE
        def _send_greeting():
            try:
                with ai_chat_lock:
                    # Sử dụng temperature thấp nhất cho greeting
                    greeting = lyra_ai.generate_stream_event_reply("greeting", temperature=0.1)
                if greeting:
                    _sse_broadcast(
                        {
                            "type": "stream_event",
                            "event": "stream_start",
                            "reply": greeting,
                            "emotion": "happy",
                            "action": "WAVE",
                            "sender_name": "Lyra",
                            "source_type": "system",
                        }
                    )
                    print(f"[Stream] Greeting: {greeting}")
            except Exception as e:
                print(f"[Stream] Greeting error: {e}")

        enqueue(PRIORITY_NORMAL, _send_greeting)

        return jsonify(result)

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/stream/stop", methods=["POST"])
def stream_stop():
    """Dừng poll YouTube Live Chat và promote regular viewers"""
    data = request.get_json() or {}
    platform = data.get("platform", "youtube")
    channel_id = data.get("channel_id", "default")

    # ── IDEA-02: Stream farewell — chạy qua background worker để không block response ──
    enqueue(PRIORITY_HIGH, _send_farewell_async, channel_id, platform)

    promoted = viewer_tracker.promote_regular_viewers(
        platform=platform, channel_id=channel_id
    )

    # Clear L2 Session Memory khi stream kết thúc
    lyra_ai.memory.clear_session_memory()

    # Viết nhật ký bí mật + Hợp nhất trí nhớ sau buổi stream (CLS) + RL Consolidation
    enqueue(PRIORITY_NORMAL, lyra_ai.write_diary_entry)
    enqueue(PRIORITY_NORMAL, lyra_ai.memory.consolidate_episodic_to_semantic)
    enqueue(PRIORITY_NORMAL, lyra_ai.rl_loop.consolidate_post_stream)

    # Reset greeted set cho session tiếp theo
    with _greeted_lock:
        _greeted_viewers_this_session.clear()

    # ── IDEA-03: Check stream milestones ──────────────────────────────────────
    try:
        # Tăng stream count trước khi check milestone — trả về số buổi stream hiện tại
        stream_num = lyra_ai.memory.increment_stream_count()

        from config import STREAM_TITLE

        # Debut (lần đầu tiên)
        if lyra_ai.memory.check_stream_milestone(
            "debut", f"Buổi stream đầu tiên của Lyra!", stream_title=STREAM_TITLE
        ):
            record_milestone("First stream debut!")

        # Mốc số lượng stream
        for milestone_n in [10, 25, 50, 100]:
            if stream_num >= milestone_n:
                if lyra_ai.memory.check_stream_milestone(
                    f"stream_{milestone_n}",
                    f"Đã stream {milestone_n} buổi!",
                    stream_title=STREAM_TITLE,
                ):
                    record_milestone(f"Reached {milestone_n} streams!")
    except Exception as e:
        print(f"[Stream] Milestone check error: {e}")

    # ── Live Context: reset to neutral (stream now officially ended) ──────────
    reset_live_context()

    result = yt_poller.stop()
    lyra_ai.is_streaming = False
    result["promoted_viewers"] = promoted
    result["promoted_count"] = len(promoted)
    return jsonify(result)


@app.route("/stream/viewers/regulars", methods=["GET"])
def stream_regular_viewers():
    """Danh sách regular viewers, filter theo platform"""
    platform = request.args.get("platform")
    limit = min(int(request.args.get("limit", 50)), 200)
    viewers = viewer_tracker.get_regular_viewers(platform=platform, limit=limit)
    return jsonify({"viewers": viewers, "count": len(viewers)})


@app.route("/stream/analytics", methods=["GET"])
def stream_analytics():
    """Thống kê sau mỗi stream: top viewers, regulars promoted, queue stats"""
    platform = request.args.get("platform", "youtube")
    channel_id = request.args.get("channel_id")
    limit = min(int(request.args.get("limit", 10)), 50)

    top_viewers = viewer_tracker.get_top_viewers(
        platform=platform, channel_id=channel_id, limit=limit
    )
    regulars = viewer_tracker.get_regular_viewers(platform=platform, limit=limit)

    # Queue stats
    queue_stats = {
        "donor_pending": _priority_queues["donor"].qsize(),
        "regular_viewer_pending": _priority_queues["regular_viewer"].qsize(),
        "new_viewer_pool": len(_new_viewer_pool),
        "reply_cooldown_s": REPLY_COOLDOWN,
        "new_viewer_interval_s": NEW_VIEWER_PICK_INTERVAL,
    }

    return jsonify(
        {
            "top_viewers": top_viewers,
            "regular_viewers": regulars,
            "stream_content": {
                "title": STREAM_TITLE,
                "game": STREAM_GAME,
                "goals": STREAM_GOALS,
                "notes": STREAM_NOTES,
            },
            "queue_stats": queue_stats,
            "yt_poller": yt_poller.get_status(),
        }
    )


@app.route("/stream/status", methods=["GET"])
def stream_status():
    """Trạng thái hiện tại của YouTube poller"""
    return jsonify(yt_poller.get_status())


@app.route("/stream/events")
def stream_events():
    """
    SSE endpoint — frontend subscribe để nhận reply của Lyra real-time.
    Frontend dùng: const es = new EventSource('/stream/events')
    """
    import queue as _queue

    def generate():
        q = _queue.Queue(maxsize=50)
        with _sse_lock:
            if len(_sse_subscribers) >= MAX_SSE_SUBSCRIBERS:
                # Quá nhiều subscriber — trả về ngay, không add vào list
                return
            _sse_subscribers.append(q)
        try:
            # Gửi heartbeat ngay để giữ connection
            yield 'data: {"type":"connected"}\n\n'
            while True:
                try:
                    msg = q.get(timeout=20)
                    yield msg
                except _queue.Empty:
                    yield ": heartbeat\n\n"  # SSE comment để giữ connection
        except GeneratorExit:
            pass
        finally:
            with _sse_lock:
                if q in _sse_subscribers:
                    _sse_subscribers.remove(q)

    return app.response_class(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# Auth routes for YouTube API access (OAuth 2.0 flow)
@app.route("/authorize")
def authorize():
    import secrets
    import hashlib
    import base64

    flow = google_auth_oauthlib.flow.Flow.from_client_secrets_file(
        CLIENT_SECRETS_FILE, scopes=SCOPES
    )
    flow.redirect_uri = url_for("oauth2callback", _external=True)

    # Tự tạo PKCE code_verifier + code_challenge (Google yêu cầu)
    code_verifier = secrets.token_urlsafe(64)
    code_challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )

    authorization_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
        code_challenge=code_challenge,
        code_challenge_method="S256",
    )

    session["state"] = state
    session["code_verifier"] = code_verifier
    return redirect(authorization_url)


@app.route("/oauth2callback")
def oauth2callback():
    state = session["state"]
    flow = google_auth_oauthlib.flow.Flow.from_client_secrets_file(
        CLIENT_SECRETS_FILE, scopes=SCOPES, state=state
    )
    flow.redirect_uri = url_for("oauth2callback", _external=True)

    code_verifier = session.pop("code_verifier", None)
    fetch_kwargs = {"authorization_response": request.url}
    if code_verifier:
        fetch_kwargs["code_verifier"] = code_verifier

    flow.fetch_token(**fetch_kwargs)

    credentials = flow.credentials
    session["credentials"] = {
        "token": credentials.token,
        "refresh_token": credentials.refresh_token,
        "token_uri": credentials.token_uri,
        "client_id": credentials.client_id,
        "client_secret": credentials.client_secret,
        "scopes": list(credentials.scopes) if credentials.scopes else [],
    }
    _save_youtube_credentials(session["credentials"])
    return "Xác thực thành công! Lyra đã có quyền truy cập YouTube."


@app.route("/secret/diary")
@limiter.limit("5 per minute")
@require_auth
def view_diary():
    """Trang xem nhật ký bí mật"""
    entries = lyra_ai.memory.get_diary_entries(limit=30)
    return render_template("diary.html", entries=entries)


@app.route("/debug/queue", methods=["GET"])
@require_auth
def debug_queue():
    """Returns the current background worker queue size."""
    stats = {
        "pending_jobs": get_queue_stats(),
        "status": "operational"
    }
    return jsonify(stats)


# ========================
# MAIN
# ========================

if __name__ == "__main__":
    print("Starting Lyra AI Server...")
    print("Sessions will be saved to: ./flask_sessions")
    # Start proactive monitor thread
    threading.Thread(target=_proactive_monitor, daemon=True).start()
    app.run(debug=False, use_reloader=False, host="127.0.0.1")
