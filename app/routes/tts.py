"""
routes/tts.py — TTS route.

  POST /speak  — Edge TTS, sentence-pipelined, phát ra VB Cable

Pipeline:
  1. Cắt reply thành câu nhỏ (split tại dấu câu)
  2. Synthesize tất cả câu song song (asyncio.gather)
  3. Push vào audio queue theo thứ tự → câu 1 phát ngay ~300ms,
     câu 2,3... đã sẵn sàng trong queue khi câu trước vừa xong
"""

from __future__ import annotations

import asyncio
import io
import re
import traceback
import unicodedata

import edge_tts
from edge_tts.exceptions import NoAudioReceived
from flask import Blueprint, jsonify, request, current_app

from config import EDGE_TTS_VOICE, EDGE_TTS_PITCH

bp = Blueprint("tts", __name__)

# Tối thiểu ký tự để một đoạn được tính là câu (tránh phát "..." hay "ừ" riêng lẻ)
MIN_SENTENCE_LEN = 4

# Regex bắt emoji và ký tự đặc biệt mà Edge TTS không xử lý được
_EMOJI_RE = re.compile(
    "["
    "\U0001F600-\U0001F64F"  # emoticons
    "\U0001F300-\U0001F5FF"  # symbols & pictographs
    "\U0001F680-\U0001F9FF"  # transport & misc
    "\U00002600-\U000026FF"  # misc symbols
    "\U00002700-\U000027BF"  # dingbats
    "\U0001FA00-\U0001FFFF"  # supplemental
    "]+",
    flags=re.UNICODE,
)


def _clean_for_tts(text: str) -> str:
    """
    Làm sạch text trước khi đưa vào Edge TTS:
    - Xoá emoji (nguyên nhân chính gây NoAudioReceived)
    - Xoá markdown formatting (*bold*, _italic_, `code`)
    - Xoá ký tự điều khiển (category C) nhưng giữ newline
    - Collapse khoảng trắng thừa
    """
    text = _EMOJI_RE.sub("", text)
    text = re.sub(r"[*_`~|]", "", text)
    text = "".join(
        ch for ch in text
        if unicodedata.category(ch)[0] not in ("C",) or ch in "\n\t"
    )
    text = re.sub(r"\s+", " ", text).strip()
    return text


def split_sentences(text: str) -> list[str]:
    """
    Cắt text thành các câu tại dấu câu kết thúc.
    Giữ dấu câu gắn với câu trước để TTS đọc đúng ngữ điệu.
    """
    # Split sau dấu .  !  ?  …  và ellipsis ...
    parts = re.split(r"(?<=[.!?…])\s+", text.strip())
    sentences = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if len(part) >= MIN_SENTENCE_LEN or not sentences:
            # Câu đủ dài, hoặc là câu đầu tiên (luôn giữ dù ngắn)
            sentences.append(part)
        else:
            # Ngắn + đã có câu trước → gộp vào để không bị mất text
            sentences[-1] += " " + part
    return sentences or [text.strip()]


async def _synthesize_one(index: int, text: str, voice: str, rate: str, pitch: str) -> tuple[int, bytes]:
    """
    Synthesize một câu, trả về (index, audio_bytes) để giữ thứ tự sau gather.
    Catch NoAudioReceived per-sentence để một câu lỗi không kill cả request.
    """
    try:
        communicate = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch)
        buf = io.BytesIO()
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                buf.write(chunk["data"])
        audio = buf.getvalue()
        if not audio:
            print(f"[TTS] Câu #{index} synthesize rỗng: {text[:40]!r}")
        return index, audio
    except NoAudioReceived:
        print(f"[TTS] NoAudioReceived cho câu #{index}: {text[:60]!r} — bỏ qua")
        return index, b""
    except Exception as e:
        print(f"[TTS] Câu #{index} lỗi ({type(e).__name__}): {e}")
        return index, b""


async def _synthesize_all(sentences: list[str], voice: str, rate: str, pitch: str) -> list[bytes]:
    """Synthesize tất cả câu song song, trả về list bytes theo đúng thứ tự."""
    tasks = [
        _synthesize_one(i, s, voice, rate, pitch)
        for i, s in enumerate(sentences)
    ]
    results = await asyncio.gather(*tasks)
    # Sort theo index để đảm bảo thứ tự dù gather hoàn thành không theo thứ tự
    results.sort(key=lambda x: x[0])
    return [audio for _, audio in results]


@bp.route("/speak", methods=["POST"])
def speak():
    audio   = current_app.audio_service
    lyra_ai = current_app.lyra_ai

    try:
        data = request.get_json()
        if not data or "text" not in data:
            return jsonify({"error": "No text provided"}), 400

        text = data["text"].strip()
        if not text:
            return jsonify({"error": "Empty text"}), 400

        # Làm sạch trước khi đưa vào TTS (xoá emoji, markdown...)
        text = _clean_for_tts(text)
        if not text:
            return jsonify({"error": "Text rỗng sau khi clean"}), 400

        print(f"[TTS] Request text: {text[:120]!r}")

        # Prosody speed mapping: attention → Edge TTS rate string
        attention = lyra_ai.emotion.attention
        if attention <= 2:
            tts_rate = "+0%"
        elif attention <= 4:
            tts_rate = "+10%"
        elif attention >= 8:
            tts_rate = "+30%"
        else:
            tts_rate = "+20%"

        sentences = split_sentences(text)
        print(f"[TTS] Split thành {len(sentences)} câu: {[s[:30] for s in sentences]}")

        # Synthesize tất cả song song
        audio_chunks = asyncio.run(
            _synthesize_all(sentences, EDGE_TTS_VOICE, tts_rate, EDGE_TTS_PITCH)
        )

        # Push vào queue theo thứ tự — câu 1 bắt đầu phát ngay,
        # các câu sau đợi trong queue khi câu trước xong
        queued = 0
        for chunk in audio_chunks:
            if chunk:
                audio.play_to_cable(chunk)
                queued += 1
            else:
                print("[TTS] Warning: một câu bị synthesize rỗng, bỏ qua.")

        if queued == 0:
            return jsonify({"error": "Tất cả câu đều synthesize thất bại"}), 500

        return jsonify({
            "ok":           True,
            "sentences":    queued,
            "audio_output": "vb_cable",
            "device_id":    audio.device_id,
        })

    except Exception:
        print("[TTS] ERROR")
        traceback.print_exc()
        return jsonify({"error": "TTS internal error"}), 500
