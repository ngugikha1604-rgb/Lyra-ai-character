import asyncio
import pyvts
import json
import os
import threading
import time
import math
import random

class VTSController:
    def __init__(self, port=8001, token_path="vts_token.json"):
        self.port = port
        self.token_path = token_path
        self.vts = None
        self.is_connected = False
        self.loop = None
        self.thread = None
        self.last_activity_time = time.time()
        self._request_lock = None  # asyncio.Lock — khởi tạo trong event loop
        
        # Mapping cảm xúc sang Hotkey ID (Tên tạm thời theo ý user)
        self.emotion_to_hotkey = {
            "neutral": "RESET",
            "content": "EXP_CONTENT",       # hài lòng nhẹ — giữa neutral và happy
            "happy": "EXP_HAPPY",
            "ecstatic": "EXP_HAPPY_MAX",
            "sad": "EXP_SAD",
            "disappointed": "EXP_SAD_MIN",
            "angry": "EXP_ANGRY",
            "furious": "EXP_ANGRY_MAX",
            "bored": "EXP_BORED",
            "thinking": "EXP_THINKING",
            "friendly": "EXP_FRIENDLY",
            "loving": "EXP_LOVING",
            "sleeping": "EXP_SLEEPING",
            "cold": "EXP_COLD",
            "observing": "EXP_OBSERVING",
        }

        self.plugin_info = {
            "plugin_name": "Lyra AI Bridge",
            "developer": "ngugikha1604",
            "authentication_token_path": self.token_path
        }

    def start(self):
        """Khởi chạy bridge trong một thread riêng"""
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()

    def _run_loop(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._main_logic())

    async def _main_logic(self):
        # Tạo Lock trong event loop — phải tạo ở đây, không phải trong __init__
        self._request_lock = asyncio.Lock()
        idle_task = None
        while True:
            try:
                print(f"[VTS] Đang kết nối tới port {self.port}...")
                self.vts = pyvts.vts(plugin_info=self.plugin_info, port=self.port)
                await self.vts.connect()
                await self.vts.request_authenticate_token()
                await self.vts.request_authenticate()
                self.is_connected = True
                print("[VTS] Kết nối thành công!")

                # Hủy idle task cũ nếu còn sót từ lần connect trước
                if idle_task and not idle_task.done():
                    idle_task.cancel()
                idle_task = asyncio.create_task(self._idle_loop())

                # Giữ kết nối sống
                while self.is_connected:
                    await asyncio.sleep(5)
            except Exception as e:
                print(f"[VTS] Lỗi kết nối: {e}")
                self.is_connected = False
                if idle_task and not idle_task.done():
                    idle_task.cancel()
                await asyncio.sleep(10)  # Thử lại sau 10s

    def trigger_emotion(self, emotion):
        """Hàm đồng bộ để gọi từ Flask"""
        if not self.is_connected or not self.loop:
            return
        self.last_activity_time = time.time()

        hotkey_id = self.emotion_to_hotkey.get(emotion.lower())
        if hotkey_id:
            asyncio.run_coroutine_threadsafe(
                self._trigger_expression_with_reset(hotkey_id), self.loop
            )

    def trigger_action(self, action):
        """Hàm đồng bộ để gọi action pose (WAVE, NOD...)."""
        if not self.is_connected or not self.loop:
            return
        if not action or action.upper() == "NONE":
            return
            
        self.last_activity_time = time.time()
        action_hotkey = f"ACT_{action.upper()}"
        asyncio.run_coroutine_threadsafe(
            self._trigger_action_with_reset(action_hotkey), self.loop
        )

    async def _trigger_action_with_reset(self, hotkey_id: str, hold_seconds: float = 1.2):
        """Trigger action hotkey.
        
        Lưu ý: Không dùng 'RESET' ở đây vì nó sẽ xóa luôn cả Expression đang có.
        """
        await self._trigger_hotkey(hotkey_id)

    async def _trigger_expression_with_reset(self, hotkey_id: str):
        """Áp dụng expression mới.
        
        User feedback: Không nên RESET trước mỗi expression vì làm mất hiệu ứng.
        VTS sẽ tự thay thế expression nếu chúng cùng group.
        """
        await self._trigger_hotkey(hotkey_id)

    def update_vad_params(self, valence: float, arousal: float, dominance: float):
        """
        VAD → Live2D Parameter Mapper (Paralinguistics — Module 5).
        """
        if not self.is_connected or not self.loop:
            return
        self.last_activity_time = time.time()

        brow_value = valence * 0.4
        eye_value = 0.76 + arousal * 1.14
        body_angle = (dominance - 0.5) * 10.0

        asyncio.run_coroutine_threadsafe(
            self._update_vad_params_async(brow_value, eye_value, body_angle),
            self.loop
        )

    async def _safe_request(self, req):
        """Serialize tất cả VTS requests qua Lock để tránh concurrent recv."""
        if self._request_lock is None:
            return None
        async with self._request_lock:
            return await self.vts.request(req)

    async def _update_vad_params_async(self, brow: float, eye: float, body_angle: float):
        """Async helper để update nhiều params cùng lúc."""
        if self.vts is None:
            return
        try:
            params = [
                ("ParamBrowLY",     brow),
                ("ParamBrowRY",     brow),
                ("ParamEyeLOpen",   eye),
                ("ParamEyeROpen",   eye),
                ("ParamBodyAngleX", body_angle),
            ]
            for name, value in params:
                req = self.vts.vts_request.requestSetParameterValue(name, value)
                await self._safe_request(req)
        except Exception as e:
            print(f"[VTS] VAD params update error: {e}")

    async def _trigger_hotkey(self, hotkey_id):
        try:
            request = self.vts.vts_request.requestTriggerHotKey(hotkey_id)
            await self._safe_request(request)
            print(f"[VTS] Triggered hotkey: {hotkey_id}")
        except Exception as e:
            print(f"[VTS] Lỗi trigger hotkey {hotkey_id}: {e}")

    def update_parameter(self, parameter_name, value):
        if not self.is_connected or not self.loop:
            return
        asyncio.run_coroutine_threadsafe(self._update_param(parameter_name, value), self.loop)

    async def _update_param(self, name, val):
        try:
            request = self.vts.vts_request.requestSetParameterValue(name, val)
            await self._safe_request(request)
        except Exception as e:
            print(f"[VTS] Lỗi cập nhật parameter {name}: {e}")

    async def _idle_loop(self):
        while self.is_connected:
            await asyncio.sleep(0.1)
            if self.vts is None:
                continue

            if time.time() - self.last_activity_time > 5.0:
                try:
                    params = []
                    t = time.time()
                    sway = math.sin(t * 1.5) * 2.0
                    params.append(("ParamBodyAngleX", sway))
                    params.append(("ParamAngleZ", sway * 1.5))
                    jitter_head_x = random.gauss(0, 0.15)
                    jitter_head_y = random.gauss(0, 0.10)
                    jitter_brow   = random.gauss(0, 0.03)
                    params.append(("ParamAngleX", jitter_head_x))
                    params.append(("ParamAngleY", jitter_head_y))
                    params.append(("ParamBrowLY", jitter_brow))
                    params.append(("ParamBrowRY", jitter_brow))
                    eye_val = 1.0
                    if random.random() < 0.03:
                        eye_val = 0.0
                    params.append(("ParamEyeLOpen", eye_val))
                    params.append(("ParamEyeROpen", eye_val))

                    for name, value in params:
                        req = self.vts.vts_request.requestSetParameterValue(name, value)
                        await self._safe_request(req)
                except Exception:
                    pass

# Singleton instance
vts_bridge = VTSController()
