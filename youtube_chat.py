# youtube_chat.py — YouTube Live Chat poller + message queue
# Giai đoạn 6: Tương tác YouTube live

import threading
import time
import queue
import re
from datetime import datetime

try:
    from googleapiclient.discovery import build
    from google.oauth2.credentials import Credentials
    YOUTUBE_API_AVAILABLE = True
except ImportError:
    YOUTUBE_API_AVAILABLE = False
    print("[YouTube] google-api-python-client not installed. YouTube polling disabled.")

# ========================
# Config
# ========================

POLL_INTERVAL_SECONDS = 4       # Poll YouTube API mỗi N giây
SLOW_MODE_COOLDOWN = 12         # Lyra chờ ít nhất N giây giữa 2 reply
MAX_QUEUE_SIZE = 20             # Tối đa N message trong queue (tránh backlog)
MENTION_KEYWORDS = ["lyra", "lira", "lyra?", "@lyra"]  # Trigger mention

# Priority score — message có score cao hơn được xử lý trước
PRIORITY_MENTION = 10
PRIORITY_TOP_VIEWER = 5         # viewer có affinity >= 2.0
PRIORITY_NORMAL = 1


class YouTubeChatPoller:
    """
    Poll YouTube Live Chat API định kỳ.
    Lọc và đưa message vào priority queue để Lyra xử lý.

    Flow:
        start(credentials, live_chat_id) → background thread poll API
        → filter & score message
        → đẩy vào self.message_queue
        → web.py consumer lấy ra và gọi /stream-chat logic
    """

    def __init__(self, viewer_tracker=None):
        self.viewer_tracker = viewer_tracker
        self.message_queue = queue.PriorityQueue(maxsize=MAX_QUEUE_SIZE)
        self._poll_thread = None
        self._stop_event = threading.Event()
        self._last_reply_time = 0.0     # timestamp của reply gần nhất
        self._next_page_token = None
        self._is_running = False
        self._live_chat_id = None
        self._credentials = None
        self._processed_ids = set()     # tránh xử lý lại message cũ
        self._stats = {
            "polled": 0,
            "queued": 0,
            "skipped_flood": 0,
            "skipped_duplicate": 0,
            "started_at": None,
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self, credentials_dict: dict, live_chat_id: str):
        """
        Bắt đầu poll YouTube Live Chat.
        credentials_dict: dict từ session['credentials'] (OAuth flow).
        live_chat_id: ID của live chat (lấy từ YouTube API hoặc URL stream).
        """
        if not YOUTUBE_API_AVAILABLE:
            raise RuntimeError("google-api-python-client not installed")

        if self._is_running:
            return {"status": "already_running", "live_chat_id": self._live_chat_id}

        self._credentials = Credentials(
            token=credentials_dict.get("token"),
            refresh_token=credentials_dict.get("refresh_token"),
            token_uri=credentials_dict.get("token_uri"),
            client_id=credentials_dict.get("client_id"),
            client_secret=credentials_dict.get("client_secret"),
            scopes=credentials_dict.get("scopes"),
        )
        self._live_chat_id = live_chat_id
        self._stop_event.clear()
        self._next_page_token = None
        self._processed_ids.clear()
        self._stats["started_at"] = datetime.now().isoformat()
        self._stats["polled"] = 0
        self._stats["queued"] = 0
        self._stats["skipped_flood"] = 0
        self._stats["skipped_duplicate"] = 0

        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._poll_thread.start()
        self._is_running = True

        print(f"[YouTube] Polling started for live_chat_id={live_chat_id}")
        return {"status": "started", "live_chat_id": live_chat_id}

    def stop(self):
        """Dừng polling"""
        self._stop_event.set()
        self._is_running = False
        print("[YouTube] Polling stopped")
        return {"status": "stopped"}

    def get_status(self) -> dict:
        return {
            "is_running": self._is_running,
            "live_chat_id": self._live_chat_id,
            "queue_size": self.message_queue.qsize(),
            "stats": self._stats,
        }

    def get_next_message(self, timeout: float = 0.1) -> dict | None:
        """
        Lấy message tiếp theo từ queue (non-blocking với timeout).
        Trả về None nếu queue rỗng.
        """
        try:
            # PriorityQueue trả về (priority, item) — priority âm để cao hơn = ưu tiên hơn
            _, item = self.message_queue.get(timeout=timeout)
            return item
        except queue.Empty:
            return None

    def mark_replied(self):
        """Gọi sau khi Lyra đã reply — reset cooldown timer"""
        self._last_reply_time = time.time()

    def can_reply(self) -> bool:
        """Kiểm tra slow mode cooldown"""
        return (time.time() - self._last_reply_time) >= SLOW_MODE_COOLDOWN

    # ------------------------------------------------------------------
    # Internal polling loop
    # ------------------------------------------------------------------

    def _poll_loop(self):
        """Background thread: poll YouTube API định kỳ"""
        if not YOUTUBE_API_AVAILABLE:
            return

        try:
            import google.auth.transport.requests as google_requests
            authed_session = google_requests.Request()
            youtube = build("youtube", "v3", credentials=self._credentials)
        except Exception as e:
            print(f"[YouTube] Failed to build API client: {e}")
            self._is_running = False
            return

        while not self._stop_event.is_set():
            try:
                # Auto-refresh token nếu hết hạn
                if self._credentials.expired and self._credentials.refresh_token:
                    self._credentials.refresh(authed_session)
                    print("[YouTube] Token refreshed")
                self._fetch_messages(youtube)
            except Exception as e:
                print(f"[YouTube] Poll error: {e}")
                # Nếu lỗi auth, dừng hẳn
                if "invalid_grant" in str(e).lower() or "unauthorized" in str(e).lower():
                    print("[YouTube] Auth error — stopping poller")
                    self._is_running = False
                    break

            self._stop_event.wait(POLL_INTERVAL_SECONDS)

    def _fetch_messages(self, youtube):
        """Gọi YouTube API lấy messages mới"""
        params = {
            "liveChatId": self._live_chat_id,
            "part": "snippet,authorDetails",
            "maxResults": 200,
        }
        if self._next_page_token:
            params["pageToken"] = self._next_page_token

        response = youtube.liveChatMessages().list(**params).execute()
        self._next_page_token = response.get("nextPageToken")
        items = response.get("items", [])
        self._stats["polled"] += len(items)

        for item in items:
            self._process_item(item)

    def _process_item(self, item: dict):
        """Xử lý 1 message từ YouTube API response"""
        msg_id = item.get("id", "")

        # Bỏ qua message đã xử lý
        if msg_id in self._processed_ids:
            self._stats["skipped_duplicate"] += 1
            return
        self._processed_ids.add(msg_id)

        # Giữ set không quá lớn
        if len(self._processed_ids) > 2000:
            self._processed_ids = set(list(self._processed_ids)[-1000:])

        snippet = item.get("snippet", {})
        author = item.get("authorDetails", {})

        # Chỉ xử lý text message
        if snippet.get("type") != "textMessageEvent":
            return

        message_text = snippet.get("textMessageDetails", {}).get("messageText", "").strip()
        if not message_text:
            return

        sender_id = author.get("channelId", "unknown")
        sender_name = author.get("displayName", "Viewer")

        # Tính priority score
        priority = self._score_message(message_text, sender_id, sender_name)

        chat_event = {
            "message": message_text,
            "sender_id": sender_id,
            "sender_name": sender_name,
            "platform": "youtube",
            "channel_id": self._live_chat_id,
            "role": "viewer",
            "priority": priority,
            "timestamp": snippet.get("publishedAt", datetime.now().isoformat()),
        }

        # Đưa vào queue — dùng priority âm vì PriorityQueue lấy nhỏ nhất trước
        try:
            self.message_queue.put_nowait((-priority, chat_event))
            self._stats["queued"] += 1
        except queue.Full:
            self._stats["skipped_flood"] += 1

    def _score_message(self, message: str, sender_id: str, sender_name: str) -> int:
        """
        Tính priority score cho message.
        Cao hơn = được xử lý trước.
        """
        score = PRIORITY_NORMAL
        msg_lower = message.lower()

        # Mention Lyra → ưu tiên cao nhất
        if any(kw in msg_lower for kw in MENTION_KEYWORDS):
            score = PRIORITY_MENTION
            return score

        # Top viewer (có affinity cao) → ưu tiên trung bình
        if self.viewer_tracker:
            info = self.viewer_tracker.get_viewer_info(sender_id, "youtube", self._live_chat_id)
            if info and info.get("affinity_score", 1.0) >= 2.0:
                score = PRIORITY_TOP_VIEWER

        return score


# ========================
# Helper: lấy live_chat_id từ video URL hoặc video ID
# ========================

def get_live_chat_id(credentials_dict: dict, video_id: str) -> str | None:
    """
    Lấy liveChatId từ video ID của YouTube stream.
    Dùng khi user cung cấp video URL thay vì liveChatId trực tiếp.
    """
    if not YOUTUBE_API_AVAILABLE:
        return None

    try:
        creds = Credentials(
            token=credentials_dict.get("token"),
            refresh_token=credentials_dict.get("refresh_token"),
            token_uri=credentials_dict.get("token_uri"),
            client_id=credentials_dict.get("client_id"),
            client_secret=credentials_dict.get("client_secret"),
            scopes=credentials_dict.get("scopes"),
        )
        youtube = build("youtube", "v3", credentials=creds)
        response = youtube.videos().list(
            part="liveStreamingDetails",
            id=video_id
        ).execute()

        items = response.get("items", [])
        if not items:
            return None

        return items[0].get("liveStreamingDetails", {}).get("activeLiveChatId")

    except Exception as e:
        print(f"[YouTube] get_live_chat_id error: {e}")
        return None
