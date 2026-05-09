"""
AudioService — quản lý toàn bộ audio pipeline cho Lyra.

Trách nhiệm:
  - Tự động phát hiện thiết bị VB-Audio Cable
  - Queue phát audio tuần tự (tránh chồng chéo giọng nói)
  - Pitch shifting để giọng trẻ hơn
  - play_to_cable: chuyển mp3 bytes → numpy → sounddevice

Không phụ thuộc Flask — có thể test độc lập.
"""

from __future__ import annotations

import io
import queue
import threading
import time
import os

import numpy as np
import sounddevice as sd
from pydub import AudioSegment


def _find_vb_cable_device() -> int:
    """Tự động tìm ID thiết bị 'CABLE Input' (VB-Audio Virtual Cable)."""
    try:
        devices = sd.query_devices()
        for i, dev in enumerate(devices):
            if "CABLE Input" in dev["name"] and dev["max_output_channels"] > 0:
                print(f"[Audio] Tìm thấy VB-Cable tại ID: {i} ({dev['name']})")
                return i
    except Exception as e:
        print(f"[Audio] Lỗi khi quét thiết bị âm thanh: {e}")
    return int(os.environ.get("VB_CABLE_DEVICE_ID", "15"))


class AudioService:
    """
    Singleton quản lý queue phát audio và các util xử lý audio.

    Sử dụng:
        from app.services.audio_service import audio_service
        audio_service.play_to_cable(mp3_bytes)
        audio_service.clear()
    """

    def __init__(self):
        self.device_id: int = _find_vb_cable_device()
        self._queue: queue.Queue = queue.Queue()
        self._worker_thread = threading.Thread(
            target=self._worker_loop, daemon=True, name="AudioWorker"
        )
        self._worker_thread.start()

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    def play_to_cable(self, audio_bytes: bytes, device_id: int | None = None) -> None:
        """
        Đẩy mp3 bytes vào queue để phát ra VB Cable (OBS capture).
        Non-blocking — trả về ngay, phát thực sự xảy ra trong worker thread.
        """
        target_device = device_id if device_id is not None else self.device_id
        try:
            audio = AudioSegment.from_file(io.BytesIO(audio_bytes), format="mp3")
            samples = np.array(audio.get_array_of_samples())
            if audio.channels == 2:
                samples = samples.reshape((-1, 2))
            samples = samples.astype(np.float32) / (2**15)
            self._queue.put((samples.tolist(), audio.frame_rate, target_device))
        except Exception as e:
            print(f"[AudioService] play_to_cable error: {e}")

    def clear(self) -> None:
        """
        Xóa hàng đợi âm thanh và dừng bài đang phát ngay lập tức.
        Dùng khi owner gửi tin mới — ngắt giữa chừng (Action Interruption).
        """
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except queue.Empty:
                break
        try:
            sd.stop()
        except Exception as e:
            print(f"[AudioService] clear() sd.stop error: {e}")

    def is_busy(self) -> bool:
        """True nếu còn audio đang chờ trong queue."""
        return not self._queue.empty()

    @staticmethod
    def apply_pitch_shift(audio_bytes: bytes, octaves: float = 0.22) -> bytes:
        """
        Tăng pitch qua sample-rate trick — giọng trẻ hơn, phù hợp Lyra 16 tuổi.
        0.22–0.25 octaves là dải tốt: nghe tự nhiên, không quá chipmunk.
        """
        try:
            audio = AudioSegment.from_file(io.BytesIO(audio_bytes), format="mp3")
            new_rate = int(audio.frame_rate * (2.0**octaves))
            shifted = audio._spawn(audio.raw_data, overrides={"frame_rate": new_rate})
            shifted = shifted.set_frame_rate(audio.frame_rate)
            out = io.BytesIO()
            shifted.export(out, format="mp3")
            return out.getvalue()
        except Exception as e:
            print(f"[AudioService] pitch_shift error: {e}")
            return audio_bytes

    # ------------------------------------------------------------------ #
    # Internal                                                             #
    # ------------------------------------------------------------------ #

    def _worker_loop(self) -> None:
        """Phát audio tuần tự — 1 câu xong mới lấy câu tiếp."""
        while True:
            try:
                audio_data, frame_rate, device_id = self._queue.get()
                samples = np.array(audio_data)
                sd.play(samples, samplerate=frame_rate, device=device_id)
                sd.wait()
                self._queue.task_done()
            except Exception as e:
                print(f"[AudioService] worker error: {e}")
                time.sleep(0.1)


# Singleton — import và dùng trực tiếp
audio_service = AudioService()
