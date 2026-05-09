"""
ProactiveService — background monitor, tự động tạo câu hỏi khi chat im lặng.

Trách nhiệm:
  - Chạy mỗi 30 giây, kiểm tra thời gian im lặng của chat
  - Nếu stream đang chạy + chat im > 2 phút → Lyra đặt câu hỏi khơi gợi
  - Không trigger khi audio đang phát (tránh ngắt giữa câu)
  - Broadcast kết quả qua SSEService

Không phụ thuộc Flask.
"""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING

from memory_utils import get_now_vn

if TYPE_CHECKING:
    from core import MiniAI
    from app.services.sse_service import SSEService
    from app.services.audio_service import AudioService


class ProactiveService:
    """
    Singleton chạy background thread theo dõi silence gap.

    Khởi tạo:
        proactive_service.init(lyra_ai, sse_service, audio_service, ai_chat_lock)
    """

    SILENCE_THRESHOLD_S = 120   # 2 phút
    CHECK_INTERVAL_S    = 30

    def __init__(self):
        self._lyra_ai: "MiniAI | None"     = None
        self._sse:     "SSEService | None" = None
        self._audio:   "AudioService | None" = None
        self._ai_lock: threading.Lock | None = None
        self._thread:  threading.Thread | None = None

    # ------------------------------------------------------------------ #
    # Setup                                                                #
    # ------------------------------------------------------------------ #

    def init(
        self,
        lyra_ai: "MiniAI",
        sse: "SSEService",
        audio: "AudioService",
        ai_chat_lock: threading.Lock,
    ) -> None:
        self._lyra_ai = lyra_ai
        self._sse     = sse
        self._audio   = audio
        self._ai_lock = ai_chat_lock

        self._thread = threading.Thread(
            target=self._monitor_loop, daemon=True, name="ProactiveMonitor"
        )
        self._thread.start()
        print("[ProactiveService] Monitor thread started.")

    # ------------------------------------------------------------------ #
    # Monitor loop                                                         #
    # ------------------------------------------------------------------ #

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
            return

        gap = (get_now_vn() - last_time).total_seconds()
        if gap <= self.SILENCE_THRESHOLD_S:
            return

        # Không ngắt khi đang phát audio
        if self._audio.is_busy():
            return

        prompt = (
            "Chat đã im lặng 2 phút. Đặt một câu hỏi ngắn, tò mò để khơi gợi "
            "mọi người tâm sự và giữ người xem ở lại."
        )
        with self._ai_lock:
            question = self._lyra_ai._call_light_model(
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

        if not question:
            return

        self._sse.broadcast({
            "type":        "proactive_question",
            "reply":       question.strip(),
            "emotion":     "thinking",
            "action":      "THINK",
            "sender_name": "Lyra",
            "source_type": "system",
        })

        # Reset timer để tránh spam
        self._lyra_ai._last_viewer_message_time = get_now_vn()
        print(f"[ProactiveService] Question sent: {question.strip()[:60]}")


# Singleton
proactive_service = ProactiveService()
