# viewer_tracker.py — Track livestream viewers/chatters
# Giai đoạn 2 & 3: Tách biệt kênh chat, track viewer, build stream context

import sqlite3
import threading
import math
from datetime import datetime

DB_PATH = "memory.db"

# Chỉ lưu message của viewer đủ "quen" để tránh DB phình to
SAVE_MESSAGE_MIN_COUNT = 3      # message_count >= 3 mới lưu message history
SAVE_MESSAGE_MIN_AFFINITY = 2.0 # hoặc affinity >= 2.0
MAX_MESSAGES_PER_VIEWER = 20    # giữ tối đa 20 message gần nhất mỗi viewer

# Đọc từ config nếu có, fallback về 20
try:
    from config import STREAM_REGULAR_MIN_MESSAGES as REGULAR_VIEWER_MIN_MESSAGES
except ImportError:
    REGULAR_VIEWER_MIN_MESSAGES = 20


class ViewerTracker:
    """
    Quản lý viewer stats cho livestream.
    Lưu vào bảng viewer_stats + viewer_messages trong memory.db.
    Hoàn toàn độc lập với MemorySystem — không can thiệp vào memory của Lyra.
    """

    def __init__(self, db_path=DB_PATH):
        self.db_path = db_path
        self.db_lock = threading.Lock()
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

                rank = self.get_viewer_rank(sender_id, platform, channel_id)
                rank_str = f", rank #{rank}" if rank > 0 else ""

                parts.append(
                    f"[VIEWER — {sender_name}] {familiarity}{rank_str}, {count} tin nhắn hôm nay."
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
        self.db_lock = threading.Lock()
        self._message_counter = 0   # đếm messages trong session hiện tại
        self._word_freq: collections.Counter = collections.Counter()
        self._emoji_freq: collections.Counter = collections.Counter()
        self._style_cache: str = ""
        self._style_cache_dirty = True
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

    def ingest(self, message: str, channel_id: str, platform: str):
        """
        Phân tích 1 message: trích words + emojis, cập nhật DB và in-memory counter.
        Gọi mỗi lần có message mới vào stream.
        """
        self._message_counter += 1
        # Không set dirty ở đây — chỉ set sau khi flush để cache có tác dụng

        # Trích emojis
        emojis = _EMOJI_RE.findall(message)
        for e in emojis:
            self._emoji_freq[e] += 1

        # Trích words (bỏ emoji, bỏ stopwords, min 2 ký tự)
        clean = _EMOJI_RE.sub("", message).lower()
        words = re.findall(r"[a-zA-ZÀ-ỹ]{2,}", clean)
        for w in words:
            if w not in _STOPWORDS:
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
