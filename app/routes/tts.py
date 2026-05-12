"""
routes/tts.py — TTS route.

  POST /speak  — FPT AI TTS, pitch shift, phát ra VB Cable
"""

from __future__ import annotations

import time
import traceback

import requests
from flask import Blueprint, jsonify, request, current_app

from config import FPT_API_KEY, FPT_TTS_URL, FPT_TTS_VOICE

bp = Blueprint("tts", __name__)


@bp.route("/speak", methods=["POST"])
def speak():
    audio    = current_app.audio_service
    lyra_ai  = current_app.lyra_ai

    try:
        data = request.get_json()
        if not data or "text" not in data:
            return jsonify({"error": "No text provided"}), 400

        text = data["text"].strip()
        if not text:
            return jsonify({"error": "Empty text"}), 400

        print(f"[TTS] Request text: {text[:120]!r}")

        # Prosody speed mapping: attention → FPT speed string
        attention = lyra_ai.emotion.attention
        if attention <= 2:
            tts_speed = "-1"
        elif attention <= 4:
            tts_speed = "0"
        elif attention >= 8:
            tts_speed = "2"
        else:
            tts_speed = "1"

        response = requests.post(
            FPT_TTS_URL,
            data=text.encode("utf-8"),
            headers={
                "api-key":      FPT_API_KEY,
                "voice":        FPT_TTS_VOICE,
                "speed":        tts_speed,
                "Content-Type": "application/octet-stream",
            },
            timeout=15,
        )

        if response.status_code != 200:
            print(f"[TTS] FPT error: {response.status_code} - {response.text}")
            return jsonify({"error": "TTS failed", "detail": response.text}), 500

        result = response.json()
        audio_url = result.get("async")
        if not audio_url:
            return jsonify({"error": "No audio URL returned", "detail": result}), 500

        # Polling: FPT xử lý async.
        # Chiến lược: kiểm tra nhanh 3 lần đầu (1s/lần), sau đó giãn ra 2s.
        # Tổng timeout ~40s — đủ cho cả câu dài và FPT bận.
        FAST_ATTEMPTS = 3      # poll nhanh: 1s interval
        SLOW_ATTEMPTS = 17     # poll chậm: 2s interval  (tổng: 3s + 34s = 37s)
        FAST_INTERVAL = 1.0
        SLOW_INTERVAL = 2.0

        audio_res   = None
        total_tries = FAST_ATTEMPTS + SLOW_ATTEMPTS

        for attempt in range(total_tries):
            interval = FAST_INTERVAL if attempt < FAST_ATTEMPTS else SLOW_INTERVAL
            time.sleep(interval)
            try:
                temp_res = requests.get(audio_url, timeout=6)
                if temp_res.status_code == 200 and temp_res.headers.get(
                    "Content-Type", ""
                ).startswith("audio/"):
                    audio_res = temp_res
                    print(f"[TTS] Audio ready sau {attempt + 1} lần poll.")
                    break
                if temp_res.status_code not in (200, 404):
                    print(f"[TTS] Polling status lạ: {temp_res.status_code}")
            except requests.exceptions.Timeout:
                print(f"[TTS] Polling request timeout (lần {attempt + 1})")
            except Exception as e:
                print(f"[TTS] Polling error: {e}")

        if not audio_res:
            print(f"[TTS] Timeout: không lấy được audio sau {total_tries} lần (~{FAST_ATTEMPTS*FAST_INTERVAL + SLOW_ATTEMPTS*SLOW_INTERVAL:.0f}s).")
            return jsonify({"error": "Audio fetch failed after timeout"}), 500

        # Pitch shift + phát ra VB Cable
        try:
            final_audio = audio.apply_pitch_shift(audio_res.content, octaves=0.22)
            audio.play_to_cable(final_audio)
        except Exception:
            audio.play_to_cable(audio_res.content)

        return jsonify({
            "ok":           True,
            "audio_output": "vb_cable",
            "device_id":    audio.device_id,
        })

    except Exception:
        print("[TTS] ERROR")
        traceback.print_exc()
        return jsonify({"error": "TTS internal error"}), 500
