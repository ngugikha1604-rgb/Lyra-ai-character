from flask import Flask, render_template, request, jsonify, session, send_file
from flask_session import Session
from core import MiniAI
from datetime import timedelta, datetime
import traceback
import json
import os
import io
import sqlite3
import requests
import pytz
from config import ELEVENLABS_API_KEY, ELEVENLABS_VOICE_ID
from dotenv import load_dotenv
load_dotenv()

app = Flask(__name__)

# ========================
# SESSION CONFIGURATION
# ========================

app.config['SESSION_TYPE'] = 'filesystem'
app.config['SESSION_PERMANENT'] = True
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=365)
app.config['SESSION_FILE_DIR'] = './flask_sessions'

os.makedirs('./flask_sessions', exist_ok=True)

Session(app)

DB_PATH = "memory.db"


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def build_state_payload(ai, time_period=None, time_gap_hours=None):
    return {
        "affection": int(round(ai.affection)),
        "mood": int(round(ai.mood)),
        "emotion": ai.emotion_from_state(),
        "time_period": time_period or getattr(ai, "time_period", "afternoon"),
        "time_gap_hours": time_gap_hours if time_gap_hours is not None else getattr(ai, "time_gap_hours", None),
    }


# ========================
# ROUTES
# ========================

@app.route("/")
def index():
    session.permanent = True
    app.permanent_session_lifetime = timedelta(days=365)
    return render_template("index.html")


@app.route("/reset")
def reset():
    """Xóa session nhưng GIỮ memory.db"""
    session.clear()
    return "Session cleared (memory.db preserved)"


@app.route("/reset-all")
def reset_all():
    """Xóa toàn bộ session + memory.db"""
    session.clear()
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    return "All cleared (session + memory)"


@app.route("/chat", methods=["POST"])
def chat():

    try:

        session.permanent = True

        data = request.get_json()

        print("Incoming request:", data)

        if not data or "message" not in data:
            return jsonify({"error": "Invalid request"}), 400

        user_input = data["message"].strip()

        if user_input == "":
            return jsonify({"reply": "Please say something."})

        # Initialize conversation memory
        if "messages" not in session:
            session["messages"] = []

        # ===== MOOD: Short-term (session) =====
        if "mood" not in session:
            session["mood"] = 0

        # ===== AFFECTION: Load từ persistent memory =====
        if "affection" not in session:
            ai_temp = MiniAI()
            session["affection"] = ai_temp.affection
            del ai_temp

        if "attention" not in session:
            session["attention"] = 5

        ai = MiniAI()

        # Load mood (short-term)
        ai.mood = session["mood"]
        
        # Load affection (from persistent memory)
        ai.affection = session["affection"]
        
        # Load attention
        ai.attention = session["attention"]

        # Generate AI reply
        result = ai.chat(user_input)

        # ===== SAVE BACK TO SESSION =====
        # Messages đã được lưu vào memory.json bởi core.py
        session["messages"] = ai.messages  # Vẫn giữ trong session để tương thích
        
        # Mood: Update nhưng không persist
        session["mood"] = round(result["mood"], 1)
        
        # Affection: Update AND persist to memory.json
        session["affection"] = round(result["affection"], 1)
        
        session["attention"] = ai.attention

        session.modified = True

        response_payload = build_state_payload(
            ai,
            time_period=result.get("time_period", "afternoon"),
            time_gap_hours=result.get("time_gap_hours", None)
        )
        response_payload["reply"] = result["reply"]

        return jsonify(response_payload)

    except Exception:

        print("ERROR OCCURRED")
        traceback.print_exc()

        return jsonify({
            "reply": "Something went wrong...",
            "emotion": "neutral",
            "affection": 50,
            "mood": 0,
            "time_period": "afternoon",
            "time_gap_hours": None
        })

#
# ========================
# TTS ROUTE
# ========================

@app.route("/speak", methods=["POST"])
def speak():
    """Gọi ElevenLabs TTS và trả về audio"""
    try:
        data = request.get_json()

        if not data or "text" not in data:
            return jsonify({"error": "No text provided"}), 400

        text = data["text"].strip()

        if not text:
            return jsonify({"error": "Empty text"}), 400

        url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}"

        headers = {
            "xi-api-key": ELEVENLABS_API_KEY,
            "Content-Type": "application/json"
        }

        payload = {
            "text": text,
            "model_id": "eleven_multilingual_v2",
            "voice_settings": {
                "stability": 0.5,
                "similarity_boost": 0.8,
                "style": 0.3,
                "use_speaker_boost": True
            }
        }

        response = requests.post(url, headers=headers, json=payload, timeout=20, verify=False)

        if response.status_code != 200:
            print(f"ElevenLabs error: {response.status_code} - {response.text}")
            return jsonify({"error": "TTS failed", "detail": response.text}), 500

        audio_buffer = io.BytesIO(response.content)
        audio_buffer.seek(0)

        return send_file(
            audio_buffer,
            mimetype="audio/mpeg",
            as_attachment=False
        )

    except Exception:
        print("TTS ERROR OCCURRED")
        traceback.print_exc()
        return jsonify({"error": "TTS internal error"}), 500


@app.route("/analytics")
def get_analytics():
    """Get analytics data from DB"""
    try:
        if not os.path.exists(DB_PATH):
            return jsonify({"emotions": {}, "moodHistory": [], "totalMessages": 0,
                            "conversationCount": 0, "userName": "Not Set", "favoriteTopics": []})

        conn = get_db()
        c = conn.cursor()

        name_row = c.execute("SELECT value FROM profile WHERE key='name'").fetchone()
        total_row = c.execute("SELECT value FROM metadata WHERE key='total_messages'").fetchone()
        topics = [r[0] for r in c.execute("SELECT value FROM facts WHERE type='topic' ORDER BY id DESC LIMIT 10")]
        conn.close()

        return jsonify({
            "emotions": {},
            "moodHistory": [],
            "totalMessages": int(total_row[0]) if total_row else 0,
            "conversationCount": 0,
            "userName": name_row[0] if name_row else "Not Set",
            "favoriteTopics": topics
        })
    except Exception as e:
        print(f"Error getting analytics: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/status")
def status():

    ai = MiniAI()

    if "mood" in session:
        ai.mood = session["mood"]
    if "affection" in session:
        ai.affection = session["affection"]
    if "attention" in session:
        ai.attention = session["attention"]

    return jsonify(build_state_payload(ai))


@app.route("/session-info")
def session_info():
    """Debug: Xem thông tin session"""
    return jsonify({
        "has_messages": "messages" in session,
        "message_count": len(session.get("messages", [])),
        "mood": session.get("mood", 0),
        "affection": session.get("affection", 50),
        "attention": session.get("attention", 5),
        "session_id": request.cookies.get('session', 'No session cookie')
    })

@app.route('/favicon.ico')
def favicon():
    return '', 204


@app.route("/history", methods=["GET"])
def get_history():
    """Trả về lịch sử chat từ DB"""
    try:
        if not os.path.exists(DB_PATH):
            return jsonify({"history": []})
        conn = get_db()
        messages = [{"role": r[0], "content": r[1]}
                    for r in conn.execute("SELECT role, content FROM conversation ORDER BY id ASC")]
        conn.close()
        return jsonify({"history": messages})
    except Exception:
        return jsonify({"history": []})


@app.route("/proactive", methods=["GET"])
def proactive():
    """Lyra chủ động nhắn khi user vắng lâu"""
    try:
        ai = MiniAI()

        # Load session state nếu có
        if "mood" in session:
            ai.mood = session["mood"]
        if "affection" in session:
            ai.affection = session["affection"]
        if "attention" in session:
            ai.attention = session["attention"]

        msg = ai.get_proactive_message()

        if not msg:
            return jsonify({"message": None, "should_show": False})

        # Cập nhật last_message_time để không spam
        ai.memory["time_tracking"]["last_message_time"] = datetime.now(
            pytz.timezone('Asia/Ho_Chi_Minh')
        ).isoformat()
        ai.save_memory()

        response_payload = build_state_payload(ai)
        response_payload.update({
            "message": msg,
            "should_show": True,
        })
        return jsonify(response_payload)

    except Exception:
        traceback.print_exc()
        return jsonify({"message": None, "should_show": False})


# ========================
# MAIN
# ========================

if __name__ == "__main__":
    print("Starting Lyra AI Server...")
    print("Sessions will be saved to: ./flask_sessions")
    app.run(debug=True)
