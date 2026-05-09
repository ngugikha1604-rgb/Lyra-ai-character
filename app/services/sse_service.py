"""
SSEService — quản lý Server-Sent Events broadcast tới frontend.

Trách nhiệm:
  - Lưu danh sách subscriber queues (mỗi tab browser = 1 queue)
  - Broadcast dict → JSON string tới tất cả subscriber
  - Tự dọn dead queues khi put_nowait thất bại
  - Giới hạn MAX_SUBSCRIBERS để tránh memory leak

Không phụ thuộc Flask — có thể test độc lập.
"""

from __future__ import annotations

import json
import queue
import threading
from typing import Generator

MAX_SSE_SUBSCRIBERS = 10


class SSEService:
    """
    Singleton quản lý SSE fan-out.

    Sử dụng:
        from app.services.sse_service import sse_service
        sse_service.broadcast({"type": "reply", "text": "..."})

        # Trong route:
        return Response(sse_service.event_stream(), mimetype="text/event-stream")
    """

    def __init__(self):
        self._subscribers: list[queue.Queue] = []
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    def broadcast(self, data: dict) -> None:
        """Push data dict tới tất cả subscriber đang kết nối."""
        msg = f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
        with self._lock:
            dead = []
            for q in self._subscribers:
                try:
                    q.put_nowait(msg)
                except Exception:
                    dead.append(q)
            for q in dead:
                self._subscribers.remove(q)

    def event_stream(self) -> Generator[str, None, None]:
        """
        Generator dùng cho Flask Response.
        Mỗi lần /stream/events được request, gọi hàm này.

        Ví dụ trong route:
            return Response(
                sse_service.event_stream(),
                mimetype="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        """
        q: queue.Queue = queue.Queue(maxsize=50)

        with self._lock:
            if len(self._subscribers) >= MAX_SSE_SUBSCRIBERS:
                # Từ chối kết nối mới khi đã đủ
                return
            self._subscribers.append(q)

        try:
            yield 'data: {"type":"connected"}\n\n'
            while True:
                try:
                    msg = q.get(timeout=20)
                    yield msg
                except queue.Empty:
                    yield ": heartbeat\n\n"  # SSE comment — giữ connection sống
        except GeneratorExit:
            pass
        finally:
            with self._lock:
                if q in self._subscribers:
                    self._subscribers.remove(q)

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)


# Singleton
sse_service = SSEService()
