# viewer_tracker.py — Track livestream viewers/chatters
# Giai đoạn 2 & 3: Tách biệt kênh chat, track viewer, build stream context

import sqlite3
import threading
import math
import time
import json
import os
import re
import collections
from dataclasses import dataclass, field
from collections import deque
from datetime import datetime
from memory import DB_PATH, DB_LOCK

# Chỉ lưu message của viewer đủ "quen" để tránh DB phình to
SAVE_MESSAGE_MIN_COUNT = 3      # message_count >= 3 mới lưu message history
SAVE_MESSAGE_MIN_AFFINITY = 2.0 # hoặc affinity >= 2.0
MAX_MESSAGES_PER_VIEWER = 20    # giữ tối đa 20 message gần nhất mỗi viewer

# Đọc từ config nếu có, fallback về 20
try:
    from config import STREAM_REGULAR_MIN_MESSAGES as REGULAR_VIEWER_MIN_MESSAGES
    from config import (
        CONSENSUS_EXCLAMATION_THRESHOLD,
        CONSENSUS_DISCUSSION_THRESHOLD,
        CONSENSUS_COOLDOWN_SECONDS,
        CONSENSUS_TOPIC_SHIFT_WINDOW,
    )
except ImportError:
    REGULAR_VIEWER_MIN_MESSAGES = 20
    CONSENSUS_EXCLAMATION_THRESHOLD = 0.30
    CONSENSUS_DISCUSSION_THRESHOLD  = 0.50
    CONSENSUS_COOLDOWN_SECONDS      = 60
    CONSENSUS_TOPIC_SHIFT_WINDOW    = 10

# ── Emoji meanings từ JSON ─────────────────────────────────────────────────────
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_EMOJI_MEANINGS_PATH = os.path.join(_BASE_DIR, "emoji_meanings.json")
try:
    with open(_EMOJI_MEANINGS_PATH, "r", encoding="utf-8") as _f:
        EMOJI_MEANINGS: dict = json.load(_f)
except Exception:
    EMOJI_MEANINGS = {}

# ── Regex helpers ──────────────────────────────────────────────────────────────
_EMOJI_RE = re.compile(
    "["
    "\U0001f600-\U0001f64f"
    "\U0001f300-\U0001f5ff"
    "\U0001f680-\U0001f6ff"
    "\U0001f900-\U0001f9ff"
    "\U0001fa70-\U0001faff"
    "\U00002702-\U000027b0"
    "]+",
    flags=re.UNICODE,
)

_STOPWORDS_VN = {
    "và", "là", "của", "có", "không", "được", "cho", "với", "trong", "này",
    "đó", "thì", "mà", "hay", "hoặc", "nhưng", "vì", "nên", "khi", "đã",
    "sẽ", "đang", "rồi", "lại", "cũng", "vẫn", "còn", "nữa", "thôi", "ạ",
    "nhé", "nha", "ơi", "à", "ừ", "uh", "ok", "okay", "the", "a", "an",
    "is", "it", "in", "on", "at", "to", "of", "and", "or", "but", "for",
    "i", "you", "he", "she", "we", "they", "my", "your", "his", "her",
}

_EXCLAMATION_SIGNALS = re.compile(
    r"\b(omg|wow|wtf|nooo*|yess*|gg|pog|lol|haha|hihi|hehe|"
    r"trời ơi|ơi trời|ôi|ối|ồ|oa|ôi giời|đỉnh|vãi|vl|cl|"
    r"xong rồi|quá đỉnh|quá trời|kinh|ghê)\b",
    re.IGNORECASE,
)


# ══════════════════════════════════════════════════════════════════════════════
# ConsensusResult — kết quả phân tích consensus
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class ConsensusResult:
    type: str           # "exclamation" | "discussion" | "emoji"
    content: str        # normalized text hoặc emoji
    percent: float      # % unique senders trong window
    unique_count: int   # số unique senders trong cluster
    total_unique: int   # tổng unique senders trong window
    velocity: float     # messages/s trong 10s gần nhất
    hint: str           # string inject vào prompt


# ══════════════════════════════════════════════════════════════════════════════
# ConsensusDetector
# ══════════════════════════════════════════════════════════════════════════════

class ConsensusDetector:
    """
    Phát hiện khi nhiều viewer đang nói cùng 1 thứ trong một khoảng thời gian.

    Hai loại consensus:
    - exclamation: message ngắn / cảm thán → tạo synthetic event cho Lyra react
    - discussion:  message dài / cùng chủ đề → chỉ inject vào context
    - emoji:       emoji spam → inject với nghĩa của emoji

    Window size động theo số active viewers:
    >= 100 viewers → 10s, >= 50 → 15s, >= 20 → 20s, >= 10 → 25s, < 10 → 30s
    """

    VELOCITY_WINDOW = 10.0  # giây để đo velocity

    def __init__(self):
        # (timestamp, sender_id, normalized_key, raw_message, is_emoji_only)
        self._window: deque = deque()
        self._lock = threading.Lock()

        # Cooldown tracking
        self._last_consensus_key: str = ""
        self._last_consensus_time: float = 0.0

        # Pending results để web.py poll
        self._pending_exclamation: "ConsensusResult | None" = None
        self._active_discussion_hint: str = ""
        self._active_velocity_hint: str = ""

    # ── Public API ─────────────────────────────────────────────────────────────

    def ingest(self, message: str, sender_id: str) -> "ConsensusResult | None":
        """
        Nhận 1 message mới, phân tích consensus.
        Trả về ConsensusResult nếu detect được, None nếu không.
        """
        now = time.time()
        normalized, is_emoji_only, dominant_emoji = self._normalize(message)

        if not normalized:
            return None

        with self._lock:
            self._window.append((now, sender_id, normalized, message, is_emoji_only, dominant_emoji))
            self._prune(now)

            active_viewers = self._count_unique_senders(now, window_s=self._dynamic_window(now))
            result = self._analyze(now, active_viewers)

            # Update velocity hint mỗi lần ingest
            self._active_velocity_hint = self._build_velocity_hint(now)

            return result

    def get_pending_exclamation(self) -> "ConsensusResult | None":
        """Lấy và clear pending exclamation event."""
        with self._lock:
            r = self._pending_exclamation
            self._pending_exclamation = None
            return r

    def get_active_discussion_hint(self) -> str:
        """Lấy discussion hint hiện tại (không clear — valid cho đến khi bị override)."""
        with self._lock:
            return self._active_discussion_hint

    def get_velocity_hint(self) -> str:
        """Lấy velocity hint hiện tại."""
        with self._lock:
            return self._active_velocity_hint

    def reset(self):
        """Reset toàn bộ state — gọi khi stream stop."""
        with self._lock:
            self._window.clear()
            self._last_consensus_key = ""
            self._last_consensus_time = 0.0
            self._pending_exclamation = None
            self._active_discussion_hint = ""
            self._active_velocity_hint = ""

    # ── Internal ───────────────────────────────────────────────────────────────

    def _dynamic_window(self, now: float) -> float:
        """Tính window size dựa trên số active viewers trong 30s gần nhất."""
        unique_30s = self._count_unique_senders(now, window_s=30.0)
        if unique_30s >= 100: return 10.0
        if unique_30s >= 50:  return 15.0
        if unique_30s >= 20:  return 20.0
        if unique_30s >= 10:  return 25.0
        return 30.0

    def _prune(self, now: float):
        """Xóa entries cũ hơn 60s (max window cần thiết)."""
        cutoff = now - 60.0
        while self._window and self._window[0][0] < cutoff:
            self._window.popleft()

    def _count_unique_senders(self, now: float, window_s: float) -> int:
        cutoff = now - window_s
        return len({e[1] for e in self._window if e[0] >= cutoff})

    def _analyze(self, now: float, active_viewers: int) -> "ConsensusResult | None":
        """Core analysis — tìm dominant cluster trong dynamic window."""
        win_s = self._dynamic_window(now)
        cutoff = now - win_s

        # Lấy entries trong window
        entries = [e for e in self._window if e[0] >= cutoff]
        if len(entries) < 3:
            return None

        total_unique = len({e[1] for e in entries})
        if total_unique < 2:
            return None

        # Tách emoji-only vs text
        emoji_entries = [e for e in entries if e[4]]   # is_emoji_only
        text_entries  = [e for e in entries if not e[4]]

        result = None

        # --- Emoji consensus ---
        if emoji_entries:
            result = self._check_emoji_consensus(emoji_entries, total_unique, now)

        # --- Text consensus ---
        if result is None and text_entries:
            result = self._check_text_consensus(text_entries, total_unique, now)

        if result is None:
            return None

        # --- Cooldown check ---
        key = result.content
        time_since_last = now - self._last_consensus_time

        if time_since_last < CONSENSUS_COOLDOWN_SECONDS:
            # Trong cooldown — chỉ process nếu topic shift
            if self._is_same_topic(key, self._last_consensus_key):
                return None  # same topic, skip

        # Update cooldown
        self._last_consensus_key = key
        self._last_consensus_time = now

        # Store result
        if result.type == "exclamation" or result.type == "emoji":
            self._pending_exclamation = result
        else:
            self._active_discussion_hint = result.hint

        return result

    def _check_emoji_consensus(self, emoji_entries: list, total_unique: int, now: float) -> "ConsensusResult | None":
        """Kiểm tra emoji spam consensus."""
        # Count by dominant emoji per unique sender
        sender_emoji: dict = {}
        for ts, sender_id, key, raw, is_emoji, dom_emoji in emoji_entries:
            if dom_emoji and sender_id not in sender_emoji:
                sender_emoji[sender_id] = dom_emoji

        if not sender_emoji:
            return None

        emoji_counter = collections.Counter(sender_emoji.values())
        dominant_emoji, count = emoji_counter.most_common(1)[0]
        percent = count / total_unique

        if percent < CONSENSUS_EXCLAMATION_THRESHOLD:
            return None

        meaning = EMOJI_MEANINGS.get(dominant_emoji, "")
        if meaning:
            meaning_str = f" ({meaning})"
            hint = (
                f"[CHAT EMOJI SPAM]: {count}/{total_unique} người đang spam {dominant_emoji}{meaning_str}. "
                f"Mọi người đang react với emoji này — Lyra nên acknowledge cả chat."
            )
        else:
            # Emoji không có trong từ điển — Lyra không biết nghĩa
            hint = (
                f"[CHAT EMOJI SPAM]: {count}/{total_unique} người đang spam {dominant_emoji}. "
                f"Đây là emoji Lyra chưa biết nghĩa — có thể hỏi chat emoji đó có nghĩa gì, "
                f"hoặc react tự nhiên theo ngữ cảnh."
            )

        velocity = self._calc_velocity(now)
        return ConsensusResult(
            type="emoji",
            content=f"EMOJI:{dominant_emoji}",
            percent=percent,
            unique_count=count,
            total_unique=total_unique,
            velocity=velocity,
            hint=hint,
        )

    def _check_text_consensus(self, text_entries: list, total_unique: int, now: float) -> "ConsensusResult | None":
        """Kiểm tra text message consensus."""
        # Group by normalized key, 1 entry per unique sender
        sender_key: dict = {}
        sender_raw: dict = {}
        for ts, sender_id, key, raw, is_emoji, dom_emoji in text_entries:
            if sender_id not in sender_key:
                sender_key[sender_id] = key
                sender_raw[sender_id] = raw

        key_counter = collections.Counter(sender_key.values())
        if not key_counter:
            return None

        dominant_key, count = key_counter.most_common(1)[0]
        percent = count / total_unique

        # Classify: exclamation hay discussion?
        raw_messages = [sender_raw[sid] for sid, k in sender_key.items() if k == dominant_key]
        consensus_type = self._classify(raw_messages)

        threshold = (
            CONSENSUS_EXCLAMATION_THRESHOLD if consensus_type == "exclamation"
            else CONSENSUS_DISCUSSION_THRESHOLD
        )
        if percent < threshold:
            return None

        velocity = self._calc_velocity(now)

        if consensus_type == "exclamation":
            hint = (
                f"[CHAT ĐỒNG THUẬN — CẢM THÁN]: {count}/{total_unique} người đang nói \"{dominant_key}\". "
                f"Mọi người đang react — Lyra nên acknowledge cả chat, không phải 1 người."
            )
        else:
            hint = (
                f"[CHAT ĐỒNG THUẬN — CHỦ ĐỀ]: {count}/{total_unique} người đang nói về \"{dominant_key}\". "
                f"Đây là chủ đề đang được thảo luận chung trong chat."
            )

        return ConsensusResult(
            type=consensus_type,
            content=dominant_key,
            percent=percent,
            unique_count=count,
            total_unique=total_unique,
            velocity=velocity,
            hint=hint,
        )

    def _classify(self, messages: list) -> str:
        """Phân loại cluster là exclamation hay discussion."""
        if not messages:
            return "discussion"
        avg_words = sum(len(m.split()) for m in messages) / len(messages)
        exclamation_hits = sum(
            1 for m in messages
            if _EXCLAMATION_SIGNALS.search(m) or m.count("!") >= 2 or m.count("?") >= 2
        )
        if avg_words < 5 or exclamation_hits / len(messages) > 0.4:
            return "exclamation"
        return "discussion"

    def _normalize(self, message: str) -> tuple:
        """
        Normalize message thành key.
        Trả về (normalized_key, is_emoji_only, dominant_emoji).
        """
        # Check emoji-only
        stripped = message.strip()
        emojis_found = _EMOJI_RE.findall(stripped)
        text_without_emoji = _EMOJI_RE.sub("", stripped).strip()

        is_emoji_only = bool(emojis_found) and len(text_without_emoji) <= 2

        dominant_emoji = None
        if emojis_found:
            # Lấy emoji xuất hiện nhiều nhất trong message
            flat = [e for group in emojis_found for e in group]
            if flat:
                dominant_emoji = collections.Counter(flat).most_common(1)[0][0]

        if is_emoji_only:
            return (f"EMOJI:{dominant_emoji}", True, dominant_emoji)

        # Text normalize
        text = text_without_emoji.lower().strip()
        words = [w for w in re.findall(r"[a-zA-ZÀ-ỹ0-9]{2,}", text) if w not in _STOPWORDS_VN]

        if not words:
            return ("", False, None)

        # Short message → exact key, long → first 3 words
        key = " ".join(words) if len(words) <= 3 else " ".join(words[:3])
        return (key, False, dominant_emoji)

    def _is_same_topic(self, new_key: str, old_key: str) -> bool:
        """So sánh 2 key có cùng topic không."""
        if new_key == old_key:
            return True
        # Emoji vs emoji
        if new_key.startswith("EMOJI:") and old_key.startswith("EMOJI:"):
            return new_key == old_key
        # Text overlap
        new_words = set(new_key.split())
        old_words = set(old_key.split())
        if not new_words or not old_words:
            return False
        overlap = len(new_words & old_words) / max(len(new_words), len(old_words))
        return overlap >= 0.6

    def _calc_velocity(self, now: float) -> float:
        """Tính messages/s trong VELOCITY_WINDOW gần nhất."""
        cutoff = now - self.VELOCITY_WINDOW
        count = sum(1 for e in self._window if e[0] >= cutoff)
        return round(count / self.VELOCITY_WINDOW, 2)

    def _build_velocity_hint(self, now: float) -> str:
        """Build velocity hint string để inject vào stream_context."""
        v = self._calc_velocity(now)
        if v > 15:
            return f"[CHAT VELOCITY]: Chat đang cực kỳ sôi nổi ({v:.0f} msg/10s)"
        if v > 5:
            return f"[CHAT VELOCITY]: Chat đang rất sôi ({v:.0f} msg/10s)"
        if v > 1:
            return f"[CHAT VELOCITY]: Chat đang hoạt động ({v:.0f} msg/10s)"
        return ""




class ViewerTracker:
    """
    Quản lý viewer stats cho livestream.
    Lưu vào bảng viewer_stats + viewer_messages trong memory.db.
    Hoàn toàn độc lập với MemorySystem — không can thiệp vào memory của Lyra.
    """

    def __init__(self, db_path=DB_PATH):
        self.db_path = db_path
        self.db_lock = DB_LOCK
        self._init_tables()
        # Cache regular_viewers để tránh DB lookup mỗi message
        self._regular_cache: dict = {}   # viewer_id+platform → dict
        self._regular_cache_ts: float = 0.0
        self._regular_cache_ttl: float = 60.0  # refresh mỗi 60 giây

    def _get_conn(self):
        conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_tables(self):
        """Tạo bảng viewer_stats, viewer_messages, regular_viewers nếu chưa có"""
        try:
            conn = self._get_conn()
            with self.db_lock:
                conn.executescript("""
                    CREATE TABLE IF NOT EXISTS viewer_stats (
                        viewer_id       TEXT NOT NULL,
                        platform        TEXT NOT NULL DEFAULT 'unknown',
                        channel_id      TEXT NOT NULL DEFAULT 'default',
                        viewer_name     TEXT NOT NULL DEFAULT 'Viewer',
                        message_count   INTEGER DEFAULT 1,
                        affinity_score  REAL DEFAULT 1.0,
                        first_seen      TEXT NOT NULL,
                        last_seen       TEXT NOT NULL,
                        PRIMARY KEY (viewer_id, platform, channel_id)
                    );

                    CREATE TABLE IF NOT EXISTS viewer_messages (
                        id          INTEGER PRIMARY KEY AUTOINCREMENT,
                        viewer_id   TEXT NOT NULL,
                        platform    TEXT NOT NULL DEFAULT 'unknown',
                        channel_id  TEXT NOT NULL DEFAULT 'default',
                        viewer_name TEXT NOT NULL,
                        message     TEXT NOT NULL,
                        sent_at     TEXT NOT NULL
                    );

                    CREATE INDEX IF NOT EXISTS idx_viewer_messages_viewer
                        ON viewer_messages (viewer_id, platform, channel_id);

                    CREATE TABLE IF NOT EXISTS regular_viewers (
                        id              INTEGER PRIMARY KEY AUTOINCREMENT,
                        viewer_id       TEXT NOT NULL,
                        platform        TEXT NOT NULL DEFAULT 'youtube',
                        viewer_name     TEXT NOT NULL,
                        total_streams   INTEGER DEFAULT 1,
                        total_messages  INTEGER DEFAULT 0,
                        affection       INTEGER DEFAULT 30,
                        first_seen      TEXT NOT NULL,
                        last_seen       TEXT NOT NULL,
                        notes           TEXT DEFAULT '',
                        UNIQUE(viewer_id, platform)
                    );
                """)
                conn.commit()
            conn.close()
        except Exception as e:
            print(f"[ViewerTracker] Init error: {e}")

    def record_message(self, sender_id: str, sender_name: str, platform: str, channel_id: str, message: str = "") -> dict:
        """
        Ghi nhận 1 message từ viewer.
        - Upsert viewer_stats: tăng message_count, tính affinity
        - Lưu vào viewer_messages nếu viewer đủ "quen"
        Trả về dict thông tin viewer hiện tại.
        """
        now = datetime.now().isoformat()

        try:
            conn = self._get_conn()
            c = conn.cursor()

            with self.db_lock:
                existing = c.execute(
                    "SELECT message_count, affinity_score FROM viewer_stats "
                    "WHERE viewer_id=? AND platform=? AND channel_id=?",
                    (sender_id, platform, channel_id)
                ).fetchone()

                if existing:
                    new_count = existing["message_count"] + 1
                    new_affinity = round(1.0 + math.log1p(new_count) * 0.5, 2)

                    c.execute(
                        "UPDATE viewer_stats SET "
                        "viewer_name=?, message_count=?, affinity_score=?, last_seen=? "
                        "WHERE viewer_id=? AND platform=? AND channel_id=?",
                        (sender_name, new_count, new_affinity, now,
                         sender_id, platform, channel_id)
                    )
                else:
                    new_count = 1
                    new_affinity = 1.0
                    c.execute(
                        "INSERT INTO viewer_stats "
                        "(viewer_id, platform, channel_id, viewer_name, message_count, affinity_score, first_seen, last_seen) "
                        "VALUES (?,?,?,?,1,1.0,?,?)",
                        (sender_id, platform, channel_id, sender_name, now, now)
                    )

                # Lưu message history chỉ với viewer đủ quen
                if message and (
                    new_count >= SAVE_MESSAGE_MIN_COUNT
                    or new_affinity >= SAVE_MESSAGE_MIN_AFFINITY
                ):
                    c.execute(
                        "INSERT INTO viewer_messages (viewer_id, platform, channel_id, viewer_name, message, sent_at) "
                        "VALUES (?,?,?,?,?,?)",
                        (sender_id, platform, channel_id, sender_name, message[:300], now)
                    )
                    # Giữ tối đa MAX_MESSAGES_PER_VIEWER message gần nhất
                    c.execute(
                        "DELETE FROM viewer_messages WHERE viewer_id=? AND platform=? AND channel_id=? "
                        "AND id NOT IN ("
                        "  SELECT id FROM viewer_messages WHERE viewer_id=? AND platform=? AND channel_id=? "
                        "  ORDER BY id DESC LIMIT ?"
                        ")",
                        (sender_id, platform, channel_id,
                         sender_id, platform, channel_id, MAX_MESSAGES_PER_VIEWER)
                    )

                conn.commit()

            conn.close()

            return {
                "viewer_id": sender_id,
                "viewer_name": sender_name,
                "platform": platform,
                "channel_id": channel_id,
                "message_count": new_count,
                "affinity_score": new_affinity,
            }

        except Exception as e:
            print(f"[ViewerTracker] record_message error: {e}")
            return {
                "viewer_id": sender_id,
                "viewer_name": sender_name,
                "message_count": 1,
                "affinity_score": 1.0,
            }

    def get_viewer_rank(self, sender_id: str, platform: str, channel_id: str) -> int:
        """
        Trả về rank của viewer theo message_count trong channel đó.
        Rank 1 = top chatter. Trả -1 nếu không tìm thấy.
        """
        try:
            conn = self._get_conn()
            c = conn.cursor()

            row = c.execute(
                "SELECT COUNT(*) as rank FROM viewer_stats "
                "WHERE platform=? AND channel_id=? AND message_count > ("
                "  SELECT message_count FROM viewer_stats "
                "  WHERE viewer_id=? AND platform=? AND channel_id=?"
                ")",
                (platform, channel_id, sender_id, platform, channel_id)
            ).fetchone()

            conn.close()
            return (row["rank"] + 1) if row else -1

        except Exception as e:
            print(f"[ViewerTracker] get_viewer_rank error: {e}")
            return -1

    def get_top_viewers(self, platform: str = None, channel_id: str = None, limit: int = 10) -> list:
        """
        Trả về top viewers theo message_count.
        Có thể filter theo platform và channel_id.
        """
        try:
            conn = self._get_conn()
            c = conn.cursor()

            query = (
                "SELECT viewer_id, viewer_name, platform, channel_id, "
                "message_count, affinity_score, last_seen FROM viewer_stats"
            )
            params = []
            conditions = []

            if platform:
                conditions.append("platform=?")
                params.append(platform)
            if channel_id:
                conditions.append("channel_id=?")
                params.append(channel_id)

            if conditions:
                query += " WHERE " + " AND ".join(conditions)

            query += " ORDER BY message_count DESC LIMIT ?"
            params.append(limit)

            rows = c.execute(query, params).fetchall()
            conn.close()

            return [dict(r) for r in rows]

        except Exception as e:
            print(f"[ViewerTracker] get_top_viewers error: {e}")
            return []

    def get_viewer_info(self, sender_id: str, platform: str, channel_id: str) -> dict | None:
        """Lấy thông tin đầy đủ của 1 viewer cụ thể"""
        try:
            conn = self._get_conn()
            row = conn.execute(
                "SELECT * FROM viewer_stats WHERE viewer_id=? AND platform=? AND channel_id=?",
                (sender_id, platform, channel_id)
            ).fetchone()
            conn.close()
            return dict(row) if row else None
        except Exception as e:
            print(f"[ViewerTracker] get_viewer_info error: {e}")
            return None

    def promote_regular_viewers(self, platform: str, channel_id: str) -> list:
        """
        Sau khi stream kết thúc: promote viewer có message_count >= REGULAR_VIEWER_MIN_MESSAGES
        lên bảng regular_viewers. Trả về danh sách viewer được promote.
        """
        promoted = []
        now = datetime.now().isoformat()
        try:
            conn = self._get_conn()
            c = conn.cursor()

            candidates = c.execute(
                "SELECT viewer_id, viewer_name, message_count, first_seen FROM viewer_stats "
                "WHERE platform=? AND channel_id=? AND message_count >= ?",
                (platform, channel_id, REGULAR_VIEWER_MIN_MESSAGES)
            ).fetchall()

            with self.db_lock:
                for row in candidates:
                    vid = row["viewer_id"]
                    vname = row["viewer_name"]
                    msgs = row["message_count"]

                    existing = c.execute(
                        "SELECT id, total_streams, total_messages, affection FROM regular_viewers "
                        "WHERE viewer_id=? AND platform=?",
                        (vid, platform)
                    ).fetchone()

                    if existing:
                        # Viewer đã quen — tăng số stream + messages, tăng affection nhẹ
                        new_streams = existing["total_streams"] + 1
                        new_msgs = existing["total_messages"] + msgs
                        # Affection tăng +5 mỗi stream, cap 85
                        new_aff = min(85, existing["affection"] + 5)
                        c.execute(
                            "UPDATE regular_viewers SET viewer_name=?, total_streams=?, "
                            "total_messages=?, affection=?, last_seen=? "
                            "WHERE viewer_id=? AND platform=?",
                            (vname, new_streams, new_msgs, new_aff, now, vid, platform)
                        )
                    else:
                        # Viewer mới được promote lần đầu
                        c.execute(
                            "INSERT INTO regular_viewers "
                            "(viewer_id, platform, viewer_name, total_streams, total_messages, "
                            "affection, first_seen, last_seen) VALUES (?,?,?,1,?,30,?,?)",
                            (vid, platform, vname, msgs, row["first_seen"] or now, now)
                        )

                    promoted.append({"viewer_id": vid, "viewer_name": vname, "message_count": msgs})

                conn.commit()
            conn.close()

            if promoted:
                print(f"[ViewerTracker] Promoted {len(promoted)} regular viewer(s): "
                      f"{[v['viewer_name'] for v in promoted]}")
            # Invalidate cache sau promote
            self._regular_cache_ts = 0.0
        except Exception as e:
            print(f"[ViewerTracker] promote_regular_viewers error: {e}")

        return promoted
    
    def clear_session_stats(self, platform: str, channel_id: str):
        """Xóa sạch bảng viewer_stats để bắt đầu buổi stream mới (giữ lại regular_viewers)"""
        try:
            conn = self._get_conn()
            with self.db_lock:
                conn.execute(
                    "DELETE FROM viewer_stats WHERE platform=? AND channel_id=?",
                    (platform, channel_id)
                )
                conn.commit()
            conn.close()
            print(f"[ViewerTracker] Session stats cleared for {platform}/{channel_id}")
        except Exception as e:
            print(f"[ViewerTracker] clear_session_stats error: {e}")

    def get_regular_viewers(self, platform: str = None, limit: int = 50) -> list:
        """Trả về danh sách regular viewers, sắp xếp theo affection giảm dần"""
        try:
            conn = self._get_conn()
            if platform:
                rows = conn.execute(
                    "SELECT * FROM regular_viewers WHERE platform=? "
                    "ORDER BY affection DESC, total_streams DESC LIMIT ?",
                    (platform, limit)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM regular_viewers ORDER BY affection DESC, total_streams DESC LIMIT ?",
                    (limit,)
                ).fetchall()
            conn.close()
            return [dict(r) for r in rows]
        except Exception as e:
            print(f"[ViewerTracker] get_regular_viewers error: {e}")
            return []

    def _refresh_regular_cache(self):
        """Reload regular_viewers từ DB vào cache"""
        import time as _t
        try:
            conn = self._get_conn()
            rows = conn.execute("SELECT * FROM regular_viewers").fetchall()
            conn.close()
            self._regular_cache = {
                f"{r['viewer_id']}:{r['platform']}": dict(r) for r in rows
            }
            self._regular_cache_ts = _t.time()
        except Exception as e:
            print(f"[ViewerTracker] cache refresh error: {e}")

    def is_regular_viewer(self, viewer_id: str, platform: str) -> dict | None:
        """
        Kiểm tra viewer có phải regular không — dùng in-memory cache.
        Trả về dict thông tin nếu có, None nếu không.
        """
        import time as _t
        if (_t.time() - self._regular_cache_ts) > self._regular_cache_ttl:
            self._refresh_regular_cache()
        return self._regular_cache.get(f"{viewer_id}:{platform}")

    def get_viewer_recent_messages(self, viewer_id: str, platform: str, channel_id: str, limit: int = 3) -> list:
        """
        Lấy N tin nhắn gần nhất của viewer từ các stream trước.
        Dùng để inject vào context khi regular viewer quay lại.
        """
        try:
            conn = self._get_conn()
            rows = conn.execute(
                "SELECT message, sent_at FROM viewer_messages "
                "WHERE viewer_id=? AND platform=? AND channel_id=? "
                "ORDER BY id DESC LIMIT ?",
                (viewer_id, platform, channel_id, limit)
            ).fetchall()
            conn.close()
            return [{"message": r["message"], "sent_at": r["sent_at"]} for r in reversed(rows)]
        except Exception as e:
            print(f"[ViewerTracker] get_viewer_recent_messages error: {e}")
            return []

    def get_stream_context(self, sender_id: str, sender_name: str, platform: str, channel_id: str, viewer_info: dict) -> str:
        """
        Build context string để inject vào prompt của Lyra.
        Phân biệt regular viewer vs viewer mới.
        """
        try:
            parts = []

            # --- Kiểm tra regular viewer ---
            regular = self.is_regular_viewer(sender_id, platform)
            if regular:
                aff = regular["affection"]
                streams = regular["total_streams"]
                parts.append(
                    f"[VIEWER QUEN — {sender_name}] "
                    f"Đã xem {streams} buổi stream. Affection: {aff}/100."
                )
                if aff >= 70:
                    parts.append("→ Viewer rất thân, có thể nhắc tên và tương tác ấm áp hơn.")
                elif aff >= 50:
                    parts.append("→ Viewer quen mặt, thân thiện tự nhiên.")
                else:
                    parts.append("→ Viewer mới được nhận ra, thân thiện nhẹ.")
            else:
                count = viewer_info.get("message_count", 1)
                affinity = viewer_info.get("affinity_score", 1.0)

                if count >= 20:
                    familiarity = "hay chat trong stream này"
                elif count >= 5:
                    familiarity = "đã chat vài lần"
                else:
                    familiarity = "viewer mới"

                parts.append(
                    f"[VIEWER — {sender_name}] {familiarity}, {count} tin nhắn hôm nay."
                )

                if affinity >= 3.0:
                    parts.append("→ Tương tác nhiều hôm nay, có thể thân thiện hơn bình thường.")

            # --- Top chatters (tối đa 3 người) ---
            top = self.get_top_viewers(platform=platform, channel_id=channel_id, limit=3)
            if top:
                names = [f"{v['viewer_name']} ({v['message_count']})" for v in top]
                parts.append(f"Top chatters hôm nay: {', '.join(names)}")

            if not parts:
                return ""

            return "[Stream context]\n" + "\n".join(f"- {p}" for p in parts)

        except Exception as e:
            print(f"[ViewerTracker] get_stream_context error: {e}")
            return ""


# ========================
# Giai đoạn 4: Chat Pattern Analyzer
# ========================

import re
import collections

# Số message tích lũy trước khi trigger stream summary
STREAM_SUMMARY_INTERVAL = 30
# Chỉ extract memory từ viewer đủ quen
EXTRACT_MIN_AFFINITY = 2.0
# Giữ tối đa N words/emojis trong style stats
TOP_N_STYLE = 8

# Stopwords tiếng Việt + tiếng Anh phổ biến — không đưa vào style hints
_STOPWORDS = {
    "và", "là", "của", "có", "không", "được", "cho", "với", "trong", "này",
    "đó", "thì", "mà", "hay", "hoặc", "nhưng", "vì", "nên", "khi", "đã",
    "sẽ", "đang", "rồi", "lại", "cũng", "vẫn", "còn", "nữa", "thôi", "ạ",
    "nhé", "nha", "ơi", "à", "ừ", "uh", "ok", "okay", "the", "a", "an",
    "is", "it", "in", "on", "at", "to", "of", "and", "or", "but", "for",
    "i", "you", "he", "she", "we", "they", "my", "your", "his", "her",
}

_EMOJI_RE = re.compile(
    "["
    "\U0001f600-\U0001f64f"
    "\U0001f300-\U0001f5ff"
    "\U0001f680-\U0001f6ff"
    "\U0001f900-\U0001f9ff"
    "\U0001fa70-\U0001faff"
    "\U00002702-\U000027b0"
    "]+",
    flags=re.UNICODE,
)


class ChatPatternAnalyzer:
    """
    Phân tích pattern của cả kênh chat:
    - Thu thập top words, top emojis từ tất cả messages
    - Build style hints để inject vào prompt
    - Trigger stream summary định kỳ
    - Quyết định có nên extract memory từ viewer này không

    Dùng chung DB với ViewerTracker (memory.db).
    """

    def __init__(self, db_path=DB_PATH):
        self.db_path = db_path
        self.db_lock = DB_LOCK
        self._message_counter = 0   # đếm messages trong session hiện tại
        self._word_freq: collections.Counter = collections.Counter()
        self._emoji_freq: collections.Counter = collections.Counter()
        self._style_cache: str = ""
        self._style_cache_dirty = True
        self._consensus = ConsensusDetector()  # ← tích hợp consensus detection
        self._init_table()

    def _get_conn(self):
        conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_table(self):
        """Tạo bảng chat_patterns nếu chưa có"""
        try:
            conn = self._get_conn()
            with self.db_lock:
                conn.executescript("""
                    CREATE TABLE IF NOT EXISTS chat_patterns (
                        id          INTEGER PRIMARY KEY AUTOINCREMENT,
                        channel_id  TEXT NOT NULL DEFAULT 'default',
                        platform    TEXT NOT NULL DEFAULT 'unknown',
                        pattern_type TEXT NOT NULL,   -- 'word' | 'emoji'
                        value       TEXT NOT NULL,
                        frequency   INTEGER DEFAULT 1,
                        updated_at  TEXT NOT NULL,
                        UNIQUE(channel_id, platform, pattern_type, value)
                    );

                    CREATE TABLE IF NOT EXISTS stream_summaries (
                        id          INTEGER PRIMARY KEY AUTOINCREMENT,
                        channel_id  TEXT NOT NULL DEFAULT 'default',
                        platform    TEXT NOT NULL DEFAULT 'unknown',
                        summary     TEXT NOT NULL,
                        message_count INTEGER DEFAULT 0,
                        created_at  TEXT NOT NULL
                    );
                """)
                conn.commit()
            conn.close()
        except Exception as e:
            print(f"[ChatPattern] Init error: {e}")

    # ------------------------------------------------------------------
    # 1. Thu thập pattern từ message
    # ------------------------------------------------------------------

    def ingest(self, message: str, channel_id: str, platform: str, sender_id: str = ""):
        """
        Phân tích 1 message: trích words + emojis, cập nhật DB và in-memory counter.
        Gọi mỗi lần có message mới vào stream.
        sender_id cần thiết cho consensus detection (unique sender tracking).
        """
        self._message_counter += 1

        # ── Consensus detection ───────────────────────────────────────────────
        if sender_id:
            self._consensus.ingest(message, sender_id)

        # Trích emojis
        emojis = _EMOJI_RE.findall(message)
        for e in emojis:
            self._emoji_freq[e] += 1

        # Trích words (bỏ emoji, bỏ stopwords, min 2 ký tự)
        clean = _EMOJI_RE.sub("", message).lower()
        words = re.findall(r"[a-zA-ZÀ-ỹ]{2,}", clean)
        for w in words:
            if w not in _STOPWORDS_VN:
                self._word_freq[w] += 1

        # Persist vào DB (batch: mỗi 10 messages để tránh write quá nhiều)
        if self._message_counter % 10 == 0:
            self._flush_patterns(channel_id, platform)

    def _flush_patterns(self, channel_id: str, platform: str):
        """Ghi top words/emojis hiện tại vào DB"""
        try:
            conn = self._get_conn()
            now = datetime.now().isoformat()
            with self.db_lock:
                for word, freq in self._word_freq.most_common(TOP_N_STYLE):
                    conn.execute(
                        "INSERT INTO chat_patterns (channel_id, platform, pattern_type, value, frequency, updated_at) "
                        "VALUES (?,?,?,?,?,?) "
                        "ON CONFLICT(channel_id, platform, pattern_type, value) DO UPDATE SET "
                        "frequency=frequency+excluded.frequency, updated_at=excluded.updated_at",
                        (channel_id, platform, "word", word, freq, now)
                    )
                for emoji, freq in self._emoji_freq.most_common(TOP_N_STYLE):
                    conn.execute(
                        "INSERT INTO chat_patterns (channel_id, platform, pattern_type, value, frequency, updated_at) "
                        "VALUES (?,?,?,?,?,?) "
                        "ON CONFLICT(channel_id, platform, pattern_type, value) DO UPDATE SET "
                        "frequency=frequency+excluded.frequency, updated_at=excluded.updated_at",
                        (channel_id, platform, "emoji", emoji, freq, now)
                    )
                conn.commit()
            conn.close()
            # Reset in-memory sau khi flush, đánh dấu cache cần rebuild
            self._word_freq.clear()
            self._emoji_freq.clear()
            self._style_cache_dirty = True
        except Exception as e:
            print(f"[ChatPattern] flush_patterns error: {e}")

    def reset_session_patterns(self, channel_id: str, platform: str):
        """Reset sạch các pattern cũ để nhận diện vibe mới của buổi stream"""
        try:
            self._word_freq.clear()
            self._emoji_freq.clear()
            self._message_counter = 0
            self._style_cache_dirty = True
            self._consensus.reset()  # reset consensus state cho stream mới

            conn = self._get_conn()
            with self.db_lock:
                conn.execute(
                    "DELETE FROM chat_patterns WHERE channel_id=? AND platform=?",
                    (channel_id, platform)
                )
                conn.commit()
            conn.close()
            print(f"[ChatPattern] session patterns reset for {platform}/{channel_id}")
        except Exception as e:
            print(f"[ChatPattern] reset_session_patterns error: {e}")

    # ── Consensus getters (proxy to ConsensusDetector) ─────────────────────────

    def get_pending_consensus_exclamation(self) -> "ConsensusResult | None":
        """Lấy và clear pending exclamation event."""
        return self._consensus.get_pending_exclamation()

    def get_active_discussion_hint(self) -> str:
        """Lấy discussion hint hiện tại."""
        return self._consensus.get_active_discussion_hint()

    def get_velocity_hint(self) -> str:
        """Lấy velocity hint hiện tại."""
        return self._consensus.get_velocity_hint()

    # ------------------------------------------------------------------
    # 2. Style hints cho prompt
    # ------------------------------------------------------------------

    def get_style_hints(self, channel_id: str, platform: str) -> str:
        """
        Trả về string ngắn mô tả vibe của kênh chat.
        Dùng để append vào stream_context trong build_prompt.
        Cache lại, chỉ rebuild khi dirty.
        """
        if not self._style_cache_dirty and self._style_cache:
            return self._style_cache

        try:
            conn = self._get_conn()
            top_words = [
                r["value"] for r in conn.execute(
                    "SELECT value FROM chat_patterns WHERE channel_id=? AND platform=? AND pattern_type='word' "
                    "ORDER BY frequency DESC LIMIT ?",
                    (channel_id, platform, TOP_N_STYLE)
                ).fetchall()
            ]
            top_emojis = [
                r["value"] for r in conn.execute(
                    "SELECT value FROM chat_patterns WHERE channel_id=? AND platform=? AND pattern_type='emoji' "
                    "ORDER BY frequency DESC LIMIT 4",
                    (channel_id, platform)
                ).fetchall()
            ]
            conn.close()

            if not top_words and not top_emojis:
                self._style_cache = ""
                return ""

            parts = []
            if top_words:
                parts.append(f"Từ hay dùng trong chat: {', '.join(top_words)}")
            if top_emojis:
                parts.append(f"Emoji phổ biến: {''.join(top_emojis)}")

            hint = "[Chat style]\n" + "\n".join(f"- {p}" for p in parts)
            self._style_cache = hint
            self._style_cache_dirty = False
            return hint

        except Exception as e:
            print(f"[ChatPattern] get_style_hints error: {e}")
            return ""

    # ------------------------------------------------------------------
    # 3. Stream summary định kỳ
    # ------------------------------------------------------------------

    def should_summarize(self) -> bool:
        """True nếu đã tích lũy đủ STREAM_SUMMARY_INTERVAL messages"""
        return self._message_counter > 0 and self._message_counter % STREAM_SUMMARY_INTERVAL == 0

    def save_stream_summary(self, summary: str, channel_id: str, platform: str):
        """Lưu stream summary vào DB"""
        try:
            conn = self._get_conn()
            now = datetime.now().isoformat()
            with self.db_lock:
                conn.execute(
                    "INSERT INTO stream_summaries (channel_id, platform, summary, message_count, created_at) "
                    "VALUES (?,?,?,?,?)",
                    (channel_id, platform, summary, self._message_counter, now)
                )
                # Giữ tối đa 10 summaries gần nhất mỗi channel
                conn.execute(
                    "DELETE FROM stream_summaries WHERE channel_id=? AND platform=? "
                    "AND id NOT IN ("
                    "  SELECT id FROM stream_summaries WHERE channel_id=? AND platform=? "
                    "  ORDER BY id DESC LIMIT 10"
                    ")",
                    (channel_id, platform, channel_id, platform)
                )
                conn.commit()
            conn.close()
        except Exception as e:
            print(f"[ChatPattern] save_stream_summary error: {e}")

    def get_recent_summaries(self, channel_id: str, platform: str, limit: int = 3) -> list:
        """Lấy các stream summary gần nhất"""
        try:
            conn = self._get_conn()
            rows = conn.execute(
                "SELECT summary, created_at FROM stream_summaries "
                "WHERE channel_id=? AND platform=? ORDER BY id DESC LIMIT ?",
                (channel_id, platform, limit)
            ).fetchall()
            conn.close()
            return [dict(r) for r in rows]
        except Exception as e:
            print(f"[ChatPattern] get_recent_summaries error: {e}")
            return []

    # ------------------------------------------------------------------
    # 4. Selective memory extraction
    # ------------------------------------------------------------------

    def should_extract_memory(self, viewer_info: dict) -> bool:
        """
        Chỉ extract memory từ viewer đủ quen.
        Tránh Lyra nhớ spam hoặc viewer random 1 lần.
        """
        affinity = viewer_info.get("affinity_score", 1.0)
        count = viewer_info.get("message_count", 1)
        return affinity >= EXTRACT_MIN_AFFINITY or count >= 5
