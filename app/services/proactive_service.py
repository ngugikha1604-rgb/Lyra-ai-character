"""
ProactiveService - background monitor that keeps stream silence from going stale.

It favors cheap template lines and only calls an LLM occasionally.
"""

from __future__ import annotations

import random
import threading
import time
from typing import TYPE_CHECKING

from config import STREAM_GAME
from memory_utils import get_now_vn
from prompts import (
    PROACTIVE_STREAM_PROMPT,
    STREAM_BANGQUA_TEMPLATES,
    STREAM_ENGAGEMENT_TEMPLATES,
)

if TYPE_CHECKING:
    from core import MiniAI
    from viewer_tracker import ChatPatternAnalyzer
    from app.services.sse_service import SSEService
    from app.services.audio_service import AudioService


class ProactiveService:
    """
    Singleton background monitor for silence gaps during streams.

    init(lyra_ai, sse_service, audio_service, ai_chat_lock, chat_analyzer=None)
    """

    SILENCE_THRESHOLD_S = 120
    CHECK_INTERVAL_S = 30

    def __init__(self):
        self._lyra_ai: "MiniAI | None" = None
        self._sse: "SSEService | None" = None
        self._audio: "AudioService | None" = None
        self._ai_lock: threading.Lock | None = None
        self._chat_analyzer: "ChatPatternAnalyzer | None" = None
        self._thread: threading.Thread | None = None

    def init(
        self,
        lyra_ai: "MiniAI",
        sse: "SSEService",
        audio: "AudioService",
        ai_chat_lock: threading.Lock,
        chat_analyzer: "ChatPatternAnalyzer | None" = None,
    ) -> None:
        self._lyra_ai = lyra_ai
        self._sse = sse
        self._audio = audio
        self._ai_lock = ai_chat_lock
        self._chat_analyzer = chat_analyzer

        self._thread = threading.Thread(
            target=self._monitor_loop, daemon=True, name="ProactiveMonitor"
        )
        self._thread.start()
        print("[ProactiveService] Monitor thread started.")

    def _monitor_loop(self) -> None:
        while True:
            time.sleep(self.CHECK_INTERVAL_S)
            try:
                self._tick()
            except Exception as e:
                print(f"[ProactiveService] error: {e}")

    def _tick(self) -> None:
        if not self._lyra_ai.is_streaming:
            return

        last_time = getattr(self._lyra_ai, "_last_viewer_message_time", None)
        if last_time is None:
            self._lyra_ai._last_viewer_message_time = get_now_vn()
            return

        gap = (get_now_vn() - last_time).total_seconds()
        if gap <= self.SILENCE_THRESHOLD_S:
            return

        if self._audio.is_busy():
            return

        question = self._choose_silence_line()
        if not question:
            return

        self._sse.broadcast({
            "type": "proactive_question",
            "reply": question.strip(),
            "emotion": "thinking",
            "action": "THINK",
            "sender_name": "Lyra",
            "source_type": "system",
        })

        self._lyra_ai._last_viewer_message_time = get_now_vn()
        print(f"[ProactiveService] Question sent: {question.strip()[:60]}")

    def _choose_silence_line(self) -> str:
        roll = random.random()

        # Task 2.5 — Ưu tiên nhắc highlight nếu có và chat đang im
        from live_context import load_live_context
        highlights = load_live_context().get("stream_highlights", [])
        if highlights and random.random() < 0.35:
            ref = random.choice(highlights)
            return f"Hồi nãy đoạn [{ref}] mọi người ồn ào thật đó, em vẫn chưa quên."

        if roll < 0.60:
            item = random.choice(STREAM_ENGAGEMENT_TEMPLATES)
            text = item["text"] if isinstance(item, dict) else str(item)
            poll = item.get("poll") if isinstance(item, dict) else None
            if poll and self._chat_analyzer:
                self._chat_analyzer.start_poll(poll[0], poll[1])
            return text

        if roll < 0.90:
            return random.choice(STREAM_BANGQUA_TEMPLATES)

        prompt = PROACTIVE_STREAM_PROMPT.format(
            current_activity="đang giữ nhịp stream",
            game=STREAM_GAME or "chuyện phiếm",
        )
        return self._lyra_ai._call_light_model(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Bạn là Lyra, VTuber 16 tuổi người Việt, đang livestream. "
                        "Lyra xưng 'em', gọi khán giả là 'mọi người', giọng tỉnh bơ hơi xéo nhưng thân thiện. "
                        "Nói 1 câu ngắn, tự nhiên, văn bản thuần, không emoji."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.4,
            max_tokens=60,
        ) or ""


proactive_service = ProactiveService()
