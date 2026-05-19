"""
StreamService — priority queue consumer cho YouTube Live Chat.

Trách nhiệm:
  - Phân loại chat event vào đúng tier: owner → donor → regular → new_viewer
  - Consumer loop chạy background: drain yt_poller → process → SSE broadcast
  - Cooldown giữa các reply
  - Consensus exclamation detection → synthetic donor event
  - Regular viewer arrival greeting (1 lần/session)
  - Gọi _handle_stream_event() để tạo AI reply và broadcast

Phụ thuộc: sse_service, audio_service (inject qua init để dễ test)
"""

from __future__ import annotations

import queue
import random
import threading
import time
import traceback
from typing import TYPE_CHECKING

from config import (
    STREAM_REPLY_COOLDOWN,
    STREAM_NEW_VIEWER_INTERVAL,
    STREAM_TITLE,
    STREAM_GAME,
    STREAM_NOTES,
)
from live_context import record_donation, record_regular_arrival
from memory_utils import get_now_vn
from background_worker import enqueue, PRIORITY_HIGH

if TYPE_CHECKING:
    from core import MiniAI
    from viewer_tracker import ViewerTracker, ChatPatternAnalyzer
    from youtube_chat import YouTubeChatPoller
    from vts_api import VTSController
    from app.services.sse_service import SSEService
    from app.services.audio_service import AudioService


class StreamService:
    """
    Singleton quản lý toàn bộ pipeline xử lý stream chat.

    Khởi tạo một lần trong app factory, inject các dependency:
        stream_service.init(lyra_ai, viewer_tracker, chat_analyzer,
                            yt_poller, vts_bridge, sse_service, audio_service,
                            ai_chat_lock)
    """

    def __init__(self):
        # Priority queues
        self._queues: dict[str, queue.Queue] = {
            "owner":          queue.Queue(maxsize=10),
            "donor":          queue.Queue(maxsize=20),
            "regular_viewer": queue.Queue(maxsize=50),
            "new_viewer":     queue.Queue(maxsize=200),
        }
        self._new_viewer_pool: list[dict] = []
        self._pool_lock = threading.Lock()

        # Greeted set — reset khi stream stop
        self._greeted_this_session: set[str] = set()
        self._greeted_lock = threading.Lock()

        # Cooldown
        self.reply_cooldown: float = float(STREAM_REPLY_COOLDOWN)
        self.new_viewer_interval: float = float(STREAM_NEW_VIEWER_INTERVAL)
        self._last_reply_time: float = 0.0
        self._last_new_viewer_pick: float = 0.0
        self._reply_lock = threading.Lock()

        # Dependencies (inject sau khi app khởi động)
        self._lyra_ai: "MiniAI | None" = None
        self._viewer_tracker: "ViewerTracker | None" = None
        self._chat_analyzer: "ChatPatternAnalyzer | None" = None
        self._yt_poller: "YouTubeChatPoller | None" = None
        self._vts_bridge: "VTSController | None" = None
        self._sse: "SSEService | None" = None
        self._audio: "AudioService | None" = None
        self._ai_lock: threading.Lock | None = None

        # Consumer thread — khởi động sau khi init()
        self._consumer_thread: threading.Thread | None = None

        # Promote timer — chạy mỗi 30 phút khi stream đang active
        self._promote_timer_thread: threading.Thread | None = None
        self._promote_stop_event = threading.Event()
        self._promote_interval_s: float = 30 * 60  # 30 phút

    # ------------------------------------------------------------------ #
    # Setup                                                                #
    # ------------------------------------------------------------------ #

    def init(
        self,
        lyra_ai: "MiniAI",
        viewer_tracker: "ViewerTracker",
        chat_analyzer: "ChatPatternAnalyzer",
        yt_poller: "YouTubeChatPoller",
        vts_bridge: "VTSController",
        sse: "SSEService",
        audio: "AudioService",
        ai_chat_lock: threading.Lock,
    ) -> None:
        """Inject dependencies và khởi động consumer thread."""
        self._lyra_ai = lyra_ai
        self._viewer_tracker = viewer_tracker
        self._chat_analyzer = chat_analyzer
        self._yt_poller = yt_poller
        self._vts_bridge = vts_bridge
        self._sse = sse
        self._audio = audio
        self._ai_lock = ai_chat_lock

        self._consumer_thread = threading.Thread(
            target=self._consumer_loop, daemon=True, name="StreamConsumer"
        )
        self._consumer_thread.start()
        print("[StreamService] Consumer thread started.")

    def start_promote_timer(self, platform: str, channel_id: str) -> None:
        """Bắt đầu promote timer — gọi khi stream start."""
        self._promote_stop_event.clear()
        self._promote_timer_thread = threading.Thread(
            target=self._promote_loop,
            args=(platform, channel_id),
            daemon=True,
            name="PromoteTimer",
        )
        self._promote_timer_thread.start()
        print(f"[StreamService] Promote timer started (every {self._promote_interval_s/60:.0f} min).")

    def stop_promote_timer(self) -> None:
        """Dừng promote timer — gọi khi stream stop."""
        self._promote_stop_event.set()
        print("[StreamService] Promote timer stopped.")

    def _promote_loop(self, platform: str, channel_id: str) -> None:
        """Chạy ngầm, promote viewer mỗi 30 phút."""
        while not self._promote_stop_event.wait(timeout=self._promote_interval_s):
            try:
                promoted = self._viewer_tracker.promote_regular_viewers(platform, channel_id)
                if promoted:
                    print(f"[PromoteTimer] Promoted {len(promoted)} viewer(s): "
                          f"{[v['viewer_name'] for v in promoted]}")
                else:
                    print("[PromoteTimer] No new viewers to promote.")
            except Exception as e:
                print(f"[PromoteTimer] error: {e}")

    # ------------------------------------------------------------------ #
    # Public helpers (dùng trong routes)                                   #
    # ------------------------------------------------------------------ #

    def reset_greeted_set(self) -> None:
        """Gọi khi stream stop/start để tránh greeting stale session."""
        with self._greeted_lock:
            self._greeted_this_session.clear()

    def get_queue_snapshot(self) -> dict:
        """Stats để hiển thị ở /stream/analytics."""
        return {
            "donor_pending":        self._queues["donor"].qsize(),
            "regular_viewer_pending": self._queues["regular_viewer"].qsize(),
            "new_viewer_pool":      len(self._new_viewer_pool),
            "reply_cooldown_s":     self.reply_cooldown,
            "new_viewer_interval_s": self.new_viewer_interval,
        }

# ------------------------------------------------------------------ #
    # Spam filter                                                          #
    # ------------------------------------------------------------------ #

    # Từ khoá xấu — tiếng Việt lẫn tiếng Anh, thêm tuỳ ý
    _BAD_KEYWORDS: frozenset[str] = frozenset([
        "dụ", "đm", "cc", "dm", "clm", "vcl", "vkl", "wtf", "fuck", "shit",
        "kill", "chết đi", "stupid", "idiot", "trash", "spam", "sub4sub",
        "t.me/", "discord.gg/", "bit.ly/", "tinyurl",
    ])
    _MIN_MSG_LEN:   int   = 2    # bỏ qua tin quá ngắn
    _MAX_EMOJI_RATIO: float = 0.7  # bỏ qua tin toàn emoji

    def _is_spam(self, message: str) -> bool:
        """
        Kiểm tra nhanh (rule-based, không dùng LLM).
        Return True nếu tin nhắn nên bị bỏ qua.
        """
        if not message:
            return True
        text = message.strip()

        # Quá ngắn
        if len(text) < self._MIN_MSG_LEN:
            return True

        text_lower = text.lower()

        # Từ khoá xấu
        for kw in self._BAD_KEYWORDS:
            if kw in text_lower:
                return True

        # Spam các ký tự lặp (vd: "aaaaaaaaaa", "!!!!!!!!")
        if len(set(text_lower.replace(" ", ""))) <= 2 and len(text) > 6:
            return True

        # Tỉ lệ emoji quá cao — đếm ký tự không phải ASCII và không phải tiếng Việt
        non_text = sum(1 for c in text if ord(c) > 0x2000)
        if len(text) > 4 and non_text / len(text) >= self._MAX_EMOJI_RATIO:
            return True

        return False

    # ------------------------------------------------------------------ #
    # Enqueue                                                              #
    # ------------------------------------------------------------------ #

    def enqueue_event(self, chat_event: dict) -> None:
        """Filter spam, phân loại event và đẩy vào đúng tier queue."""
        message   = chat_event.get("message", "")
        is_owner  = chat_event.get("is_owner", False)
        is_donor  = chat_event.get("is_donor", False)

        # Donor và owner luôn được xử lý, không filter spam
        if not is_owner and not is_donor and self._is_spam(message):
            return

        sender_id = chat_event.get("sender_id", "")
        platform  = chat_event.get("platform", "youtube")

        regular = self._viewer_tracker.is_regular_viewer(sender_id, platform)

        if is_owner:
            tier = "owner"
        elif is_donor:
            tier = "donor"
        elif regular:
            tier = "regular_viewer"
            chat_event["_regular_data"] = dict(regular)
        else:
            tier = "new_viewer"

        chat_event["_tier"]        = tier
        chat_event["_enqueued_at"] = time.time()   # ← TTL timestamp

        if tier == "new_viewer":
            with self._pool_lock:
                # Dedup: giữ tin mới nhất cho mỗi sender
                self._new_viewer_pool[:] = [
                    e for e in self._new_viewer_pool
                    if e.get("sender_id") != sender_id
                ]
                self._new_viewer_pool.append(chat_event)
                if len(self._new_viewer_pool) > 100:
                    self._new_viewer_pool.pop(0)
        else:
            try:
                self._queues[tier].put_nowait(chat_event)
            except queue.Full:
                print(
                    f"[StreamService] {tier} queue full, drop: "
                    f"{chat_event.get('sender_name')}"
                )

    # ------------------------------------------------------------------ #
    # Consumer loop                                                        #
    # ------------------------------------------------------------------ #

    def _can_reply(self) -> bool:
        return (time.time() - self._last_reply_time) >= self.reply_cooldown

    def _mark_replied(self) -> None:
        with self._reply_lock:
            self._last_reply_time = time.time()

    def _consumer_loop(self) -> None:
        while True:
            try:
                poller_running = bool(self._yt_poller and self._yt_poller._is_running)

                # Drain yt_poller → priority queues
                if poller_running:
                    while True:
                        raw = self._yt_poller.get_next_message(timeout=0.05)
                        if raw is None:
                            break
                        self.enqueue_event(raw)

                # Consensus exclamation → synthetic donor event
                consensus_event = self._chat_analyzer.get_pending_consensus_exclamation()
                if consensus_event is not None:
                    synthetic = {
                        "message":     consensus_event.hint,
                        "sender_id":   "__consensus__",
                        "sender_name": "Chat",
                        "_tier":       "donor",
                        "_is_consensus":    True,
                        "_consensus_type":  consensus_event.type,
                    }
                    try:
                        self._queues["donor"].put_nowait(synthetic)
                        print(
                            f"[StreamService] Consensus queued: {consensus_event.type} "
                            f"({consensus_event.unique_count}/{consensus_event.total_unique} = "
                            f"{consensus_event.percent:.0%})"
                        )
                    except queue.Full:
                        pass

                has_owner = not self._queues["owner"].empty()
                if not has_owner and not self._can_reply():
                    time.sleep(0.3)
                    continue

                event = self._pick_next_event()

                if event is None:
                    time.sleep(0.3)
                    continue

                self._handle_event(event)
                if event.get("_tier") != "owner":
                    self._mark_replied()

            except Exception as e:
                print(f"[StreamService] consumer error: {e}")
                time.sleep(1)

    # TTL cho tin non-donor chờ trong queue (giây)
    _MSG_TTL_S: float = 120.0  # 2 phút
    # Tỷ lệ chọn regular_viewer vs new_viewer (khi cả 2 có người)
    _REGULAR_WEIGHT: float = 0.75

    def _pick_next_event(self) -> dict | None:
        """
        Ưu tiên: donor → owner → weighted(regular 75%, new 25%) → None
        Non-donor messages quá TTL bị skip.
        """
        now = time.time()

        # Tier 1: donor — phải trả lời tất cả
        try:
            return self._queues["donor"].get_nowait()
        except queue.Empty:
            pass

        # Tier 2: owner
        try:
            return self._queues["owner"].get_nowait()
        except queue.Empty:
            pass

        # Tier 3: weighted random giữa regular_viewer và new_viewer
        has_regular  = not self._queues["regular_viewer"].empty()
        with self._pool_lock:
            pool_ready = (
                len(self._new_viewer_pool) > 0
                and (now - self._last_new_viewer_pick) >= self.new_viewer_interval
            )

        if has_regular or pool_ready:
            # Quyết định chọn tier
            if has_regular and pool_ready:
                pick_regular = random.random() < self._REGULAR_WEIGHT
            else:
                pick_regular = has_regular

            if pick_regular:
                # Drain regular queue, skip tin quá TTL
                while True:
                    try:
                        event = self._queues["regular_viewer"].get_nowait()
                        age = now - event.get("_enqueued_at", now)
                        if age <= self._MSG_TTL_S:
                            return event
                        print(
                            f"[StreamService] regular_viewer TTL expired ({age:.0f}s), "
                            f"skip: {event.get('sender_name')}"
                        )
                    except queue.Empty:
                        break
                # Sau khi drain hết — fallback sang new_viewer nếu có
                if not pool_ready:
                    return None

            # Pick new_viewer
            with self._pool_lock:
                if self._new_viewer_pool:
                    event = random.choice(self._new_viewer_pool)
                    self._new_viewer_pool.remove(event)
                    self._last_new_viewer_pick = now
                    return event

        return None

    # ------------------------------------------------------------------ #
    # Event handler                                                        #
    # ------------------------------------------------------------------ #

    def _handle_event(self, chat_event: dict) -> None:
        """Xử lý 1 event → gọi lyra_ai.chat() → SSE broadcast."""
        try:
            from app.helpers import build_state_payload, build_stream_context

            message     = chat_event["message"]
            sender_id   = chat_event["sender_id"]
            sender_name = chat_event.get("sender_name", "Viewer")
            platform    = chat_event.get("platform", "youtube")
            channel_id  = chat_event.get("channel_id", "default")
            is_consensus = chat_event.get("_is_consensus", False)
            tier        = chat_event.get("_tier", "new_viewer")

            # ── Consensus: không record viewer stats ──────────────────────────
            if is_consensus:
                source_type_val = "new_viewer"
                viewer_data     = {"viewer_name": "Chat"}
                stream_ctx      = build_stream_context(
                    self._lyra_ai, self._viewer_tracker, self._chat_analyzer,
                    sender_id, sender_name, platform, channel_id, viewer_info={}
                )
                velocity_hint = self._chat_analyzer.get_velocity_hint()
                if velocity_hint:
                    stream_ctx = f"{stream_ctx}\n{velocity_hint}" if stream_ctx else velocity_hint

                self._lyra_ai._last_viewer_message_time = get_now_vn()
                with self._ai_lock:
                    result = self._lyra_ai.chat(
                        message,
                        source_type=source_type_val,
                        viewer_data=viewer_data,
                        stream_context=stream_ctx,
                    )
                payload = build_state_payload(self._lyra_ai, result)
                payload.update({
                    "sender_id":   "__consensus__",
                    "sender_name": "Chat",
                    "source_type": "consensus",
                    "is_consensus": True,
                })
                self._sse.broadcast(payload)
                return

            # ── Normal event ──────────────────────────────────────────────────
            composed_input = message if tier == "owner" else f"[{sender_name}]: {message}"
            print(f"[StreamService] [{tier}] {sender_name}: {message}")

            viewer_info = self._viewer_tracker.record_message(
                sender_id, sender_name, platform, channel_id, message
            )
            self._chat_analyzer.ingest(message, channel_id, platform, sender_id=sender_id)
            self._lyra_ai.rl_loop.ingest_viewer_message(message, sender_name)

            stream_ctx = build_stream_context(
                self._lyra_ai, self._viewer_tracker, self._chat_analyzer,
                sender_id, sender_name, platform, channel_id, viewer_info
            )
            # Thêm discussion + velocity hints
            disc_hint = self._chat_analyzer.get_active_discussion_hint()
            if disc_hint:
                stream_ctx = f"{stream_ctx}\n{disc_hint}" if stream_ctx else disc_hint
            vel_hint = self._chat_analyzer.get_velocity_hint()
            if vel_hint:
                stream_ctx = f"{stream_ctx}\n{vel_hint}" if stream_ctx else vel_hint

            # Regular viewer arrival greeting
            if tier == "regular_viewer" and viewer_info.get("message_count", 0) == 1:
                stream_ctx = self._inject_arrival_hint(
                    chat_event, sender_id, sender_name, stream_ctx
                )

            if not self._chat_analyzer.should_extract_memory(viewer_info):
                self._lyra_ai._skip_next_memory_extraction = True

            source_type_val, viewer_data = self._resolve_viewer_data(
                tier, chat_event, sender_name
            )

            # Live context: donations
            if tier == "donor":
                record_donation(
                    viewer_name=sender_name,
                    amount=chat_event.get("donate_amount", ""),
                )

            self._lyra_ai._last_viewer_message_time = get_now_vn()
            with self._ai_lock:
                result = self._lyra_ai.chat(
                    composed_input,
                    source_type=source_type_val,
                    viewer_data=viewer_data,
                    stream_context=stream_ctx,
                )

            if self._chat_analyzer.should_summarize():
                enqueue(PRIORITY_HIGH, self._trigger_summary, channel_id, platform)

            payload = build_state_payload(self._lyra_ai, result)
            payload.update({
                "sender_id":            sender_id,
                "sender_name":          sender_name,
                "channel_id":           channel_id,
                "platform":             platform,
                "source_type":          source_type_val,
                "viewer_message_count": viewer_info.get("message_count", 1),
                "viewer_affinity":      viewer_info.get("affinity_score", 1.0),
            })
            self._sse.broadcast(payload)

        except Exception:
            print("[StreamService] handle_event error:")
            traceback.print_exc()

    # ------------------------------------------------------------------ #
    # Helpers                                                              #
    # ------------------------------------------------------------------ #

    def _inject_arrival_hint(
        self, chat_event: dict, sender_id: str, sender_name: str, stream_ctx: str
    ) -> str:
        """Inject regular viewer arrival hint nếu chưa chào trong session này."""
        with self._greeted_lock:
            if sender_id in self._greeted_this_session:
                return stream_ctx
            self._greeted_this_session.add(sender_id)

        from prompts import REGULAR_VIEWER_ARRIVAL_HINT
        regular_data = chat_event.get("_regular_data") or {}
        platform     = chat_event.get("platform", "youtube")
        channel_id   = chat_event.get("channel_id", "default")

        arrival_hint = REGULAR_VIEWER_ARRIVAL_HINT.format(
            viewer_name=sender_name,
            total_streams=regular_data.get("total_streams", 1),
            affection=regular_data.get("affection", 35),
        )
        record_regular_arrival(
            viewer_name=sender_name,
            total_streams=regular_data.get("total_streams", 1),
            affection=regular_data.get("affection", 35),
        )

        # Background: extract profile từ recent messages nếu chưa có profile
        if not self._viewer_tracker.get_viewer_profile(sender_id, platform):
            recent_msgs = self._viewer_tracker.get_viewer_recent_messages(
                sender_id, platform, channel_id, limit=10
            )
            if recent_msgs:
                enqueue(
                    PRIORITY_HIGH,
                    self._viewer_tracker.extract_and_save_profile,
                    sender_id, platform, sender_name,
                    recent_msgs, self._lyra_ai._call_light_model,
                )

        return f"{stream_ctx}\n{arrival_hint}" if stream_ctx else arrival_hint

    def _resolve_viewer_data(
        self, tier: str, chat_event: dict, sender_name: str
    ) -> tuple[str, dict | None]:
        """Trả về (source_type, viewer_data) theo tier."""
        regular_data = chat_event.get("_regular_data")
        gender       = chat_event.get("gender", "male")

        if tier == "owner":
            return "owner", None
        if tier == "donor":
            return "donor", {
                "viewer_name": sender_name,
                "affection":   regular_data["affection"] if regular_data else 40,
                "amount":      chat_event.get("donate_amount", ""),
                "gender":      gender,
            }
        if tier == "regular_viewer":
            return "regular_viewer", {
                "viewer_name":  sender_name,
                "affection":    regular_data["affection"] if regular_data else 35,
                "total_streams": regular_data["total_streams"] if regular_data else 1,
                "gender":       gender,
            }
        # new_viewer
        return "new_viewer", {"viewer_name": sender_name, "gender": gender}

    def _sync_vts(self, result: dict | None) -> None:
        """Đồng bộ emotion/action/vad sang VTube Studio."""
        if not result:
            return
        if result.get("emotion"):
            self._vts_bridge.trigger_emotion(result["emotion"])
        if result.get("action"):
            self._vts_bridge.trigger_action(result["action"])
        if result.get("vad"):
            v, a, d = result["vad"]
            self._vts_bridge.update_vad_params(v, a, d)

    def _trigger_summary(self, channel_id: str, platform: str) -> None:
        """Gọi AI tóm tắt chat — chạy trong background worker."""
        try:
            recent      = self._chat_analyzer.get_recent_summaries(channel_id, platform, limit=1)
            prev_summary = recent[0]["summary"] if recent else ""
            style        = self._chat_analyzer.get_style_hints(channel_id, platform)
            top_viewers  = self._viewer_tracker.get_top_viewers(
                platform=platform, channel_id=channel_id, limit=5
            )
            top_names = (
                ", ".join(v["viewer_name"] for v in top_viewers) if top_viewers else "chưa có"
            )
            prompt = (
                f"Đây là thông tin về buổi livestream:\n"
                f"- Top chatters: {top_names}\n{style}\n"
            )
            if prev_summary:
                prompt += f"- Summary trước: {prev_summary}\n"
            prompt += "\nTóm tắt ngắn (1-2 câu) chat đang nói về gì và vibe của kênh lúc này."

            # Không cần ai_lock — _call_light_model không đụng trạng thái lyra_ai
            summary = self._lyra_ai._call_light_model(
                [
                    {"role": "system", "content": "Bạn là assistant tóm tắt livestream chat. Trả lời bằng tiếng Việt, ngắn gọn."},
                    {"role": "user",   "content": prompt},
                ],
                temperature=0.3,
                max_tokens=80,
                provider="background"
            )
            if summary:
                summary = summary.strip()
                self._chat_analyzer.save_stream_summary(summary, channel_id, platform)
                self._lyra_ai.memory.add_item("episodic", f"[Stream] {summary}", weight=1.1, limit=12)
                try:
                    self._lyra_ai.memory.add_session_item(f"[Stream vibe] {summary[:160]}", kind="session")
                except Exception:
                    pass
                print(f"[StreamService] Summary: {summary}")
        except Exception as e:
            print(f"[StreamService] summary error: {e}")


# Singleton
stream_service = StreamService()
