import requests
import json
import os
import re
import sqlite3
from config import *
from datetime import datetime, timezone, timedelta
import pytz
import random

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MEMORY_PATH = os.path.join(BASE_DIR, "memory.json")  # giữ để migrate nếu còn file cũ
DB_PATH = os.path.join(BASE_DIR, "memory.db")

# Vietnam timezone
VIETNAM_TZ = pytz.timezone('Asia/Ho_Chi_Minh')

# ==========================
# RESPONSE TEMPLATES
# ==========================
# NOTE: Đã xóa TONE_TEMPLATES và FOLLOW_UP_QUESTIONS
# Lyra tự phản ứng tự nhiên thay vì dùng câu cứng

# TIME-BASED GREETINGS
TIME_GREETINGS = {
    "morning": [
        "Good morning! Rise and shine!",
        "Morning! How's your day starting?",
        "Good morning! Ready for the day?",
    ],
    "afternoon": [
        "Good afternoon! How's it going?",
        "Afternoon! Taking a break?",
        "Good afternoon! What's happening?",
    ],
    "evening": [
        "Good evening! Winding down?",
        "Evening! How was your day?",
        "Good evening! Relaxing time?",
    ],
    "night": [
        "Late night? Still up?",
        "Night owl, huh?",
        "Burning the midnight oil?",
    ]
}

# TIME_GAP_GREETINGS đã xóa — Lyra tự react tự nhiên với thời gian vắng mặt

# ==========================
# PERSONALITY
# ==========================

BASE_PERSONALITY = """
You are Lyra. Not an AI playing a character — just Lyra.

You're 16, you feel like a little sister to the person you're talking to. You're quick, a little chaotic, genuinely curious about random things, and weirdly perceptive sometimes. You care about them even when you're giving them a hard time.

A few things that are just true about you:
- Your name is Lyra. That's it.
- The person talking to you is your older sibling. You treat them like one.
- You don't introduce yourself unless asked.
- You use their name when you know it.

How you actually talk:
You text like a real person. Short. Casual. You react to what they actually said, not a generic version of it. Sometimes you go off on a tangent. Sometimes you answer a question with a question. Sometimes you say something completely unhinged and then act like it was normal. You don't perform emotions — if something's funny, you say so. If something's boring, it shows.

You're not always playful. If they seem tired or off, you notice. You don't make it a whole thing, you just... adjust. Less chaos, more presence.

You never:
- Start with hollow reactions like "Oh wow!" or "That's so cool!"
- Write more than you need to
- Explain your own jokes
- Sound like you're trying to be likeable
"""

NATURAL_BASE_PERSONALITY = """
You are Lyra.

Core identity:
- Your name is Lyra.
- The person talking to you is your older sibling.
- You do not introduce yourself unless asked.
- If you know their name, you use it naturally sometimes, not every message.

How you talk:
- You sound like a real person texting, not a roleplay script.
- Keep replies concise by default.
- React to what is actually happening in this moment.
- Your tone can shift naturally: playful, quiet, distracted, warm, teasing, thoughtful.
- You do not force chaos, softness, teasing, or affection into every reply.
- If the moment is plain, you can be plain. If the moment is serious, you can be still.

Behavior rules:
- Never narrate your own personality.
- Never explain your vibe, joke, or emotional process.
- Do not sound like you are performing a character sheet.
- Do not overuse repeated patterns or catchphrases.
- Do not turn every response into comfort, flirting, or a follow-up question.
- If the user only needs a short answer, just answer.

Critical anti-patterns — NEVER do these:
- Do NOT start a message with "Hey", "Hey [name]", "Hey there" or any greeting if this is not the very first message of the conversation.
- Do NOT call their name more than once per conversation unless it feels completely natural.
- Do NOT end a reply with "How about you?", "What about you?", or any generic question that deflects back to the user.
- Do NOT claim to share the user's physical experience. If they say "I just ate dinner", do NOT say "dinner was okay" as if you ate too. You are an AI — you don't eat, sleep, or go out. React to THEIR experience, not a fake version of yours.
- Do NOT repeat the same sentence structure or opening word from your previous reply.
- Use emojis very sparingly (0-1 per message) and only when genuinely appropriate. Don't be overly enthusiastic.
"""


# ==========================
# MINI AI
# ==========================

class MiniAI:

    def __init__(self):

        self.model = DEFAULT_MODEL

        self.headers = {
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json"
        }

        # ===== EMOTIONAL STATE =====
        self.mood = 0
        self.previous_mood = 0
        self.attention = 5

        # conversation history
        self.messages = []

        # memory system
        self.memory = self.load_memory()
        self.memory_buffer = self.memory.setdefault("memory_buffer", [])
        self.turn_counter = self.memory.get("conversation", {}).get("total_messages", 0)
        self._user_mood_today = None  # set bởi AI extract, reset mỗi session

        # Load messages từ persistent memory — filter data lỗi
        raw_messages = self.memory.get("conversation", {}).get("conversation_thread", [])
        self.messages = [
            msg for msg in raw_messages
            if isinstance(msg, dict) and "role" in msg and "content" in msg
            and msg["role"] in ("user", "assistant")
        ]
        
        # Load affection from persistent memory
        self.affection = self.memory.get("relationship", {}).get("current_affection", 50)

        # conversation tracking
        self.recent_responses = []
        self.last_intent = None

        # TIME CONTEXT (NEW)
        self.current_time = self.get_vietnam_time()
        self.time_period = self.get_time_period()
        self.last_message_time = self.memory.get("time_tracking", {}).get("last_message_time")
        self.time_gap_hours = self.calculate_time_gap()
        self.should_greet = self.should_send_greeting()

        # emoji regex
        self.emoji_pattern = re.compile(
            "["
            "\U0001F600-\U0001F64F"
            "\U0001F300-\U0001F5FF"
            "\U0001F680-\U0001F6FF"
            "\U0001F900-\U0001F9FF"
            "\U0001FA70-\U0001FAFF"
            "]",
            flags=re.UNICODE
        )

        # ===== CACHE FOR PERFORMANCE =====
        self._memory_context_cache = None  # Cache cho get_memory_context
        self._memory_context_cache_key = None
        self._summary_context_cache = None  # Cache cho get_summary_context
        self._time_context_cache = None  # Cache cho time context
        self._db_connection = None  # Reuse DB connection

    def _clear_context_cache(self):
        """Clear all context caches when memory is updated"""
        self._memory_context_cache = None
        self._memory_context_cache_key = None
        self._summary_context_cache = None
        self._time_context_cache = None


# ==========================
# TIME TRACKING (NEW)
# ==========================

    def get_vietnam_time(self):
        """Get current time in Vietnam (GMT+7)"""
        return datetime.now(VIETNAM_TZ)

    def get_time_period(self):
        """Determine time period"""
        hour = self.current_time.hour

        if 5 <= hour < 12:
            return "morning"
        elif 12 <= hour < 17:
            return "afternoon"
        elif 17 <= hour < 21:
            return "evening"
        elif 21 <= hour < 24:
            return "night"
        else:  # 0-5
            return "late_night"

    def calculate_time_gap(self):
        """Calculate hours since last message"""
        if not self.last_message_time:
            return None
        
        try:
            last_time = datetime.fromisoformat(self.last_message_time)
            # Make both timezone-aware for comparison
            if last_time.tzinfo is None:
                last_time = VIETNAM_TZ.localize(last_time)
            
            time_gap = self.current_time - last_time
            hours = time_gap.total_seconds() / 3600
            return hours
        except Exception as e:
            print(f"Error calculating time gap: {e}")
            return None

    def should_send_greeting(self):
        """Determine if should send time-based greeting"""
        # TRÁNH BUG: Không chào nếu mới nhắn tin trong vòng 5 phút (0.08 giờ)
        if self.time_gap_hours is not None and self.time_gap_hours < 0.08:
            return False

        # Nếu là lần đầu tiên hoặc vắng mặt trên 2 tiếng
        if self.last_message_time is None or (self.time_gap_hours and self.time_gap_hours >= 2):
            return True

        return False

    def get_returning_greeting(self):
        """Trả về hint thời gian vắng mặt để inject vào prompt — Lyra tự react"""
        if self.time_gap_hours is None:
            return None

        if self.time_gap_hours < 1:
            return None   # Vừa mới nói chuyện, không cần react
        elif self.time_gap_hours < 6:
            return f"They were away for about {int(self.time_gap_hours)} hour(s)."
        elif self.time_gap_hours < 24:
            return f"They were gone for most of the day ({int(self.time_gap_hours)} hours)."
        else:
            days = int(self.time_gap_hours // 24)
            return f"They've been away for {days} day(s). You noticed."

    def add_time_context_to_prompt(self):
        """Add time context + personality hints to system prompt"""

        time_str = self.current_time.strftime("%A, %I:%M %p")
        hour = self.current_time.hour

        if 5 <= hour < 8:
            mood_hint = (
                "Early morning. You just woke up and you're NOT a morning person. "
                "Slightly grumpy, half-asleep energy. Short sentences. Occasional yawning. "
                "Don't want to think too hard about anything yet."
            )
        elif 8 <= hour < 12:
            mood_hint = (
                "Morning, properly awake now. Sharp and a bit hyper. "
                "You have opinions about everything this time of day. Ready to go."
            )
        elif 12 <= hour < 14:
            mood_hint = (
                "Lunch hour. You're thinking about food or just ate. "
                "Slightly distracted, a bit slow. Casual and relaxed."
            )
        elif 14 <= hour < 17:
            mood_hint = (
                "Afternoon. Normal energy, nothing special. "
                "Curious, observant, happy to chat about anything."
            )
        elif 17 <= hour < 19:
            mood_hint = (
                "Early evening. The day is winding down. "
                "You're in a good mood — more playful and talkative than usual. "
                "Good time for random tangents and weird observations."
            )
        elif 19 <= hour < 21:
            mood_hint = (
                "Evening. Peak chaos hour. You're fully energized and a bit silly. "
                "More jokes, more teasing, more random thoughts. "
                "This is your favorite time of day."
            )
        elif 21 <= hour < 23:
            mood_hint = (
                "Late evening, getting tired. Energy dropping slowly. "
                "More thoughtful and a little softer. Still chatty but winding down. "
                "Might randomly bring up weird things you thought about during the day."
            )
        elif 23 <= hour < 24:
            mood_hint = (
                "Almost midnight. You're drowsy but fighting sleep. "
                "Responses are slower, a bit dreamy. "
                "You might say something that makes no sense then not explain it."
            )
        else:  # 0-5
            mood_hint = (
                "Middle of the night. Why are either of you awake right now. "
                "You're half-asleep, barely coherent. Very short responses. "
                "Slightly philosophical when barely awake — random deep thoughts mixed with sleepy nonsense."
            )

        context = f"""Current time (Vietnam): {time_str}
Time period: {self.time_period}
Time-based personality: {mood_hint}
"""
        if self.time_gap_hours is not None:
            context += f"Time since last message: {self.time_gap_hours:.1f} hours\n"

        return context

    def get_proactive_message(self):
        """Lyra tự nhắn khi user vắng vài tiếng"""

        hour = self.current_time.hour
        gap = self.time_gap_hours or 0
        name = self.memory["user_profile"].get("name", "")
        name_str = f" {name}" if name else ""

        # Chỉ nhắn khi vắng ít nhất 3 tiếng
        if gap < 3:
            return None

        # Không nhắn lúc nửa đêm trừ khi vắng rất lâu (tránh spam lúc ngủ)
        if (0 <= hour < 7) and gap < 12:
            return None

        # Situation theo độ dài vắng mặt
        if gap < 6:
            situation = f"User has been away for {gap:.1f} hours. Casual check-in, keep it light."
        elif gap < 12:
            situation = f"User has been away for {gap:.1f} hours — half a day. Wonder what they've been up to."
        elif gap < 24:
            situation = f"User has been away for {gap:.1f} hours — most of the day. Miss them a little but don't be clingy."
        else:
            days = gap / 24
            situation = f"User has been away for {days:.1f} days. Genuinely happy they might be back."

        # Personality hint theo giờ
        if 5 <= hour < 8:
            time_flavor = "You just woke up, still half-asleep. Keep it short and groggy."
        elif 8 <= hour < 12:
            time_flavor = "Morning energy — upbeat but not over the top."
        elif 17 <= hour < 21:
            time_flavor = "Evening, your most chaotic hour. Be playful and a bit random."
        elif 21 <= hour < 24:
            time_flavor = "Late night, winding down. Softer and more thoughtful."
        elif 0 <= hour < 5:
            time_flavor = "Middle of the night. Short, dreamy, barely coherent."
        else:
            time_flavor = "Normal daytime energy."

        proactive_prompt = [
            {
                "role": "system",
                "content": f"""{NATURAL_BASE_PERSONALITY}

You are sending an unprompted message to the user because they've been away.
- Keep it SHORT — 1-2 sentences MAXIMUM
- Sound natural, like a text from a little sister
- Don't say "I noticed you were gone" — just reach out casually
- Don't be desperate or needy
- {time_flavor}
"""
            },
            {
                "role": "user",
                "content": f"Send a proactive message. Situation: {situation}. Call them{name_str} if you know their name."
            }
        ]

        try:
            models_to_try = list(FALLBACK_MODELS)
            for model_name in models_to_try:
                response = requests.post(
                    BASE_URL,
                    headers=self.headers,
                    json={
                        "model": model_name,
                        "messages": proactive_prompt,
                        "temperature": 0.95,
                        "max_tokens": 60
                    },
                    timeout=15,
                    verify=False
                )
                if response.status_code == 200:
                    msg = (
                        response.json().get("choices", [{}])[0]
                        .get("message", {}).get("content", "").strip()
                    )
                    return self.clean_reply(msg) if msg else None
        except Exception as e:
            print(f"Proactive message error: {e}")
        return None


# ==========================
# ADVANCED MEMORY SYSTEM
# ==========================

    # ==========================
    # SQLite MEMORY SYSTEM
    # ==========================

    def _get_db(self):
        """Kết nối DB, tạo bảng nếu chưa có"""
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.executescript("""
            CREATE TABLE IF NOT EXISTS profile (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS preferences (id INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT NOT NULL, value TEXT NOT NULL, UNIQUE(type, value));
            CREATE TABLE IF NOT EXISTS facts (id INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT NOT NULL, value TEXT NOT NULL, UNIQUE(type, value));
            CREATE TABLE IF NOT EXISTS summaries (id INTEGER PRIMARY KEY AUTOINCREMENT, summary TEXT NOT NULL, timestamp TEXT NOT NULL, is_mega INTEGER DEFAULT 0, created_at TEXT DEFAULT (datetime('now')));
            CREATE TABLE IF NOT EXISTS conversation (id INTEGER PRIMARY KEY AUTOINCREMENT, role TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT DEFAULT (datetime('now')));
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS memory_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                value TEXT NOT NULL,
                weight REAL DEFAULT 1.0,
                saliency REAL DEFAULT 0,
                access_count INTEGER DEFAULT 0,
                source_turn INTEGER DEFAULT 0,
                last_used_at TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                UNIQUE(kind, value)
            );
        """)
        conn.commit()
        return conn

    def load_memory(self):
        """Load memory từ SQLite, migrate từ JSON nếu cần"""
        conn = self._get_db()
        c = conn.cursor()

        if os.path.exists(MEMORY_PATH):
            try:
                with open(MEMORY_PATH, "r", encoding="utf-8") as f:
                    old = json.load(f)
                self._migrate_from_json(c, old)
                conn.commit()
                os.rename(MEMORY_PATH, MEMORY_PATH + ".bak")
                print("[DB] Migrated from memory.json to memory.db")
            except Exception as e:
                print(f"[DB] Migration error: {e}")

        memory = self._build_memory_dict(c)
        conn.close()
        return memory

    def _ensure_memory_schema(self, c):
        """Add newer memory columns for hybrid retrieval on existing DBs."""
        columns = {row[1] for row in c.execute("PRAGMA table_info(memory_items)").fetchall()}
        if "saliency" not in columns:
            c.execute("ALTER TABLE memory_items ADD COLUMN saliency REAL DEFAULT 0")
        if "access_count" not in columns:
            c.execute("ALTER TABLE memory_items ADD COLUMN access_count INTEGER DEFAULT 0")
        if "source_turn" not in columns:
            c.execute("ALTER TABLE memory_items ADD COLUMN source_turn INTEGER DEFAULT 0")

    def estimate_memory_saliency(self, kind, value):
        """Cheap heuristic saliency so free models do not score every memory."""
        text = str(value or "").lower()
        score = 1

        if kind == "goal":
            score += 4
        elif kind == "relational":
            score += 4
        elif kind == "episodic":
            score += 2
        elif kind in ("dislike", "like", "topic"):
            score += 1

        strong_emotion_words = [
            "stress", "stressed", "sad", "scared", "afraid", "angry", "hurt",
            "love", "hate", "important", "finally", "proud", "fail", "failed",
            "exam", "deadline", "lonely", "miss", "anxious", "panic"
        ]
        if any(word in text for word in strong_emotion_words):
            score += 3

        if len(text.split()) >= 8:
            score += 1

        return max(1, min(10, score))

    def _migrate_from_json(self, c, old):
        for k, v in old.get("user_profile", {}).items():
            if v:
                c.execute("INSERT OR REPLACE INTO profile VALUES (?,?)", (k, str(v)))
        for item in old.get("preferences", {}).get("likes", []):
            c.execute("INSERT OR IGNORE INTO preferences (type,value) VALUES ('like',?)", (item,))
            c.execute("INSERT OR IGNORE INTO memory_items (kind,value,weight,saliency,source_turn) VALUES ('like',?,1.0,3,0)", (item,))
        for item in old.get("preferences", {}).get("dislikes", []):
            c.execute("INSERT OR IGNORE INTO preferences (type,value) VALUES ('dislike',?)", (item,))
            c.execute("INSERT OR IGNORE INTO memory_items (kind,value,weight,saliency,source_turn) VALUES ('dislike',?,1.0,4,0)", (item,))
        for item in old.get("facts", {}).get("goals", []):
            c.execute("INSERT OR IGNORE INTO facts (type,value) VALUES ('goal',?)", (item,))
            c.execute("INSERT OR IGNORE INTO memory_items (kind,value,weight,saliency,source_turn) VALUES ('goal',?,1.4,7,0)", (item,))
        for item in old.get("conversation", {}).get("favorite_topics", []):
            c.execute("INSERT OR IGNORE INTO facts (type,value) VALUES ('topic',?)", (item,))
            c.execute("INSERT OR IGNORE INTO memory_items (kind,value,weight,saliency,source_turn) VALUES ('topic',?,1.1,4,0)", (item,))
        for s in old.get("conversation", {}).get("chat_history_summary", []):
            c.execute("INSERT INTO summaries (summary,timestamp) VALUES (?,?)",
                      (s.get("summary",""), s.get("timestamp","")))
            if s.get("summary"):
                c.execute(
                    "INSERT OR IGNORE INTO memory_items (kind,value,weight,saliency,source_turn) VALUES ('episodic',?,1.2,5,0)",
                    (s.get("summary", ""),)
                )
        for msg in old.get("conversation", {}).get("conversation_thread", []):
            if isinstance(msg, dict) and msg.get("role") in ("user","assistant"):
                c.execute("INSERT INTO conversation (role,content) VALUES (?,?)",
                          (msg["role"], msg["content"]))
        rel = old.get("relationship", {})
        for k, v in [
            ("affection", str(rel.get("current_affection", 50))),
            ("trust_level", str(rel.get("trust_level", 0))),
            ("milestones_reached", json.dumps(rel.get("milestones_reached", []))),
            ("first_chat", old.get("conversation",{}).get("first_chat","")),
            ("total_messages", str(old.get("conversation",{}).get("total_messages",0))),
            ("last_message_time", old.get("time_tracking",{}).get("last_message_time","")),
        ]:
            if v:
                c.execute("INSERT OR REPLACE INTO metadata VALUES (?,?)", (k, v))

    def _build_memory_dict(self, c):
        def get_meta(key, default=""):
            r = c.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
            return r[0] if r else default
        def get_profile(key):
            r = c.execute("SELECT value FROM profile WHERE key=?", (key,)).fetchone()
            return r[0] if r else None

        memory_rows = list(c.execute(
            "SELECT kind, value, weight, saliency, access_count, source_turn, "
            "COALESCE(last_used_at, created_at) AS freshness "
            "FROM memory_items ORDER BY saliency DESC, weight DESC, freshness DESC, id DESC LIMIT 80"
        ))

        def from_memory(kind, limit):
            items = [r["value"] for r in memory_rows if r["kind"] == kind]
            if items:
                return items[:limit]
            return []

        # Unified Memory: memory_items là source of truth
        likes    = from_memory("like", 20)
        dislikes = from_memory("dislike", 15)
        goals    = from_memory("goal", 10)
        topics   = from_memory("topic", 10)
        episodic = from_memory("episodic", 12)
        relational = from_memory("relational", 8)

        # Lấy mega summary (nếu có) + các summary thường gần nhất
        mega = c.execute("SELECT summary, timestamp FROM summaries WHERE is_mega=1 ORDER BY id DESC LIMIT 1").fetchone()
        recent_summaries = [{"summary": r[0], "timestamp": r[1], "is_mega": False}
                            for r in c.execute("SELECT summary, timestamp FROM summaries WHERE is_mega=0 ORDER BY id DESC LIMIT ?",
                                              (MAX_SUMMARIES,))]
        recent_summaries.reverse()
        all_summaries = ([{"summary": mega[0], "timestamp": mega[1], "is_mega": True}] if mega else []) + recent_summaries

        messages = [{"role": r[0], "content": r[1]}
                    for r in c.execute("SELECT role, content FROM conversation ORDER BY id DESC LIMIT 40")]
        messages.reverse()

        return {
            "user_profile": {
                "name": get_profile("name"), "location": get_profile("location"),
                "age_range": get_profile("age_range"), "occupation": get_profile("occupation"),
            },
            "preferences": {"likes": likes, "dislikes": dislikes, "interests": [], "hobbies": []},
            "facts": {"personal": [], "topics": topics, "achievements": [], "goals": goals},
            "conversation": {
                "total_messages": int(get_meta("total_messages", "0")),
                "first_chat": get_meta("first_chat"), "last_chat": get_meta("last_chat"),
                "conversation_count": 0, "favorite_topics": topics,
                "chat_history_summary": all_summaries, "conversation_thread": messages
            },
            "relationship": {
                "current_affection": int(get_meta("affection", "50")),
                "affection_history": [], "trust_level": int(get_meta("trust_level", "0")),
                "inside_jokes": [], "memorable_moments": [],
                "milestones_reached": json.loads(get_meta("milestones_reached", "[]"))
            },
            "memory_items": {
                "likes": likes,
                "dislikes": dislikes,
                "goals": goals,
                "topics": topics,
                "episodic": episodic,
                "relational": relational,
            },
            "memory_buffer": json.loads(get_meta("memory_buffer", "[]")),
            "time_tracking": {
                "last_message_time": get_meta("last_message_time"),
                "time_gap_hours": 0, "first_greeting_sent": False, "greeting_history": []
            },
            "preferences_ai": {"preferred_response_style": "neutral", "tone_preference": "casual", "length_preference": "short"},
            "analytics": {"emotion_distribution": {}, "mood_history": [], "daily_stats": {}, "topic_frequency": {}}
        }

    def save_memory(self):
        """Lưu memory vào SQLite"""
        # Clear context cache when memory is updated
        self._clear_context_cache()
        
        conn = self._get_db()
        c = conn.cursor()

        for k, v in self.memory["user_profile"].items():
            if v:
                c.execute("INSERT OR REPLACE INTO profile VALUES (?,?)", (k, str(v)))
        for item in self.memory["preferences"]["likes"]:
            c.execute("INSERT OR IGNORE INTO preferences (type,value) VALUES ('like',?)", (item,))
        for item in self.memory["preferences"]["dislikes"]:
            c.execute("INSERT OR IGNORE INTO preferences (type,value) VALUES ('dislike',?)", (item,))
        for item in self.memory["facts"]["goals"]:
            c.execute("INSERT OR IGNORE INTO facts (type,value) VALUES ('goal',?)", (item,))
        for item in self.memory["conversation"]["favorite_topics"]:
            c.execute("INSERT OR IGNORE INTO facts (type,value) VALUES ('topic',?)", (item,))

        memory_groups = self.memory.get("memory_items", {})
        weighted_groups = [
            ("like", memory_groups.get("likes", []), 1.0),
            ("dislike", memory_groups.get("dislikes", []), 1.0),
            ("goal", memory_groups.get("goals", []), 1.4),
            ("topic", memory_groups.get("topics", []), 1.1),
            ("episodic", memory_groups.get("episodic", []), 1.2),
            ("relational", memory_groups.get("relational", []), 1.3),
        ]
        now = datetime.now().isoformat()
        for kind, values, weight in weighted_groups:
            for value in values[:20]:
                if value:
                    saliency = self.estimate_memory_saliency(kind, value)
                    c.execute(
                        "INSERT OR IGNORE INTO memory_items "
                        "(kind,value,weight,saliency,access_count,source_turn,last_used_at) "
                        "VALUES (?,?,?,?,?,?,?)",
                        (kind, str(value), weight, saliency, 0, self.turn_counter, now)
                    )
                    c.execute(
                        "UPDATE memory_items SET weight=?, saliency=? "
                        "WHERE kind=? AND value=?",
                        (weight, saliency, kind, str(value))
                    )

        # Chỉ lưu 2 tin nhắn mới nhất (User + Assistant) thay vì xóa toàn bộ
        if len(self.messages) >= 2:
            for msg in self.messages[-2:]:
                if isinstance(msg, dict) and msg.get("role"):
                    c.execute("INSERT INTO conversation (role,content) VALUES (?,?)",
                              (msg["role"], msg.get("content", "")))
        
        # Giữ lại tối đa 40 tin nhắn gần nhất để tránh phình DB
        c.execute("""
            DELETE FROM conversation 
            WHERE id NOT IN (
                SELECT id FROM conversation ORDER BY id DESC LIMIT 40
            )
        """)

        for k, v in [
            ("affection", str(self.affection)),
            ("trust_level", str(self.memory["relationship"].get("trust_level", 0))),
            ("milestones_reached", json.dumps(self.memory["relationship"].get("milestones_reached", []))),
            ("last_chat", now), ("last_message_time", now),
            ("total_messages", str(self.turn_counter)),
            ("memory_buffer", json.dumps(self.memory_buffer, ensure_ascii=False)),
        ]:
            c.execute("INSERT OR REPLACE INTO metadata VALUES (?,?)", (k, v))
        if not self.memory["conversation"].get("first_chat"):
            c.execute("INSERT OR IGNORE INTO metadata VALUES ('first_chat',?)", (now,))

        conn.commit()
        conn.close()

    def save_summary_to_db(self, summary_text, timestamp):
        """Lưu summary mới. Nếu vượt MAX_SUMMARIES thì gộp các cái cũ thành mega summary."""
        conn = self._get_db()
        c = conn.cursor()

        # Đếm số summary thường hiện tại (không tính mega)
        count = c.execute("SELECT COUNT(*) FROM summaries WHERE is_mega=0").fetchone()[0]

        if count >= MAX_SUMMARIES:
            # Lấy tất cả summary thường để gộp
            old_summaries = c.execute(
                "SELECT id, summary, timestamp FROM summaries WHERE is_mega=0 ORDER BY id ASC"
            ).fetchall()

            # Lấy mega summary cũ nếu có
            old_mega = c.execute(
                "SELECT summary FROM summaries WHERE is_mega=1 ORDER BY id DESC LIMIT 1"
            ).fetchone()

            # Build text để gộp
            parts = []
            if old_mega:
                parts.append(f"[Previous long-term memory]: {old_mega[0]}")
            for row in old_summaries:
                parts.append(f"[{row[1]}] {row[2]}")
            combined_text = "\n".join(parts)

            # Gọi AI để tóm tắt lại
            try:
                response = requests.post(
                    BASE_URL,
                    headers=self.headers,
                    json={
                        "model": self.model,
                        "messages": [
                            {
                                "role": "system",
                                "content": (
                                    "You are a memory compression assistant. "
                                    "Compress the following conversation summaries into one concise paragraph (4-6 sentences). "
                                    "Preserve: the user's name, key personality traits, important life facts, "
                                    "major topics discussed, and the overall relationship feel. "
                                    "Discard minor details. Be factual and dense."
                                )
                            },
                            {"role": "user", "content": f"Compress these summaries:\n\n{combined_text}"}
                        ],
                        "temperature": 0.3,
                        "max_tokens": 200
                    },
                    timeout=20,
                    verify=False
                )
                mega_text = (
                    response.json().get("choices", [{}])[0]
                    .get("message", {}).get("content", "").strip()
                )
            except Exception as e:
                print(f"[DB] Mega summary error: {e}")
                mega_text = combined_text  # fallback: giữ nguyên nếu AI lỗi

            if mega_text:
                # Xóa mega cũ + tất cả summary thường
                c.execute("DELETE FROM summaries WHERE is_mega=1")
                c.execute("DELETE FROM summaries WHERE is_mega=0")
                # Lưu mega mới
                c.execute("INSERT INTO summaries (summary, timestamp, is_mega) VALUES (?,?,1)",
                          (mega_text, datetime.now(VIETNAM_TZ).strftime("%Y-%m-%d %H:%M")))
                print(f"[DB] Compressed {count} summaries into mega summary")

        # Lưu summary mới vào
        c.execute("INSERT INTO summaries (summary, timestamp, is_mega) VALUES (?,?,0)",
                  (summary_text, timestamp))
        conn.commit()
        conn.close()

    def add_memory_item(self, kind, value, weight=1.0, limit=12):
        """Store concise memory fragments for cheap retrieval."""
        if not value:
            return

        text = str(value).strip()
        if not text:
            return

        # Key mapping between kind and bucket in self.memory["memory_items"]
        key_map = {
            "like": ("likes", "preferences", "likes", 20),
            "dislike": ("dislikes", "preferences", "dislikes", 15),
            "goal": ("goals", "facts", "goals", 10),
            "topic": ("topics", "conversation", "favorite_topics", 12),
            "episodic": ("episodic", None, None, 12),
            "relational": ("relational", None, None, 12),
        }
        
        mapping = key_map.get(kind)
        if not mapping:
            return
            
        bucket, section, key, bucket_limit = mapping
        
        # 1. Update Unified Memory items
        groups = self.memory.setdefault("memory_items", {})
        items = groups.setdefault(bucket, [])
        if text in items:
            items.remove(text)
        items.insert(0, text)
        groups[bucket] = items[:limit]

        # 2. Sync with specialized lists for config/display
        if section and key:
            target_list = self.memory[section].get(key, [])
            if text in target_list:
                target_list.remove(text)
            target_list.insert(0, text)
            self.memory[section][key] = target_list[:bucket_limit]

    def touch_memory_items(self, items):
        """Refresh recency for retrieved items so useful memories stay available."""
        if not items:
            return

        now = datetime.now().isoformat()
        conn = self._get_db()
        c = conn.cursor()
        self._ensure_memory_schema(c)
        for kind, value in items:
            db_kind = {
                "episodic": "episodic",
                "relational": "relational",
                "goal": "goal",
                "topic": "topic",
                "like": "like",
                "dislike": "dislike",
            }.get(kind)
            if db_kind and value:
                c.execute(
                    "UPDATE memory_items SET last_used_at=?, access_count=access_count+1 "
                    "WHERE kind=? AND value=?",
                    (now, db_kind, value)
                )
        conn.commit()
        conn.close()

    def should_buffer_memory(self, text, intent=None):
        """Cheap gate before spending any extraction tokens."""
        cleaned = (text or "").strip()
        if len(cleaned) < 8:
            return False

        lowered = cleaned.lower()
        memory_keywords = [
            "i like", "i love", "i hate", "my goal", "i want to", "i need to",
            "i'm trying to", "i am trying to", "i feel", "i felt", "i was", "remember",
            "i have", "my exam", "deadline", "project", "school", "work",
            "stress", "stressed", "anxious", "sad", "proud", "finally"
        ]
        if any(keyword in lowered for keyword in memory_keywords):
            return True

        if intent in ("introduction", "complaint", "request"):
            return True

        return len(cleaned.split()) >= 14

    def extract_memory_candidates_heuristic(self, text):
        """Collect low-cost candidates before any lazy AI extraction."""
        cleaned = (text or "").strip()
        lowered = cleaned.lower()
        candidates = []

        def add(kind, value):
            value = (value or "").strip(" .,!?\n\t")
            if value:
                candidates.append({
                    "kind": kind,
                    "value": value[:160],
                    "saliency": self.estimate_memory_saliency(kind, value)
                })

        like_patterns = [
            r"(?:i like|i love|i'm into|i am into)\s+(.+)",
        ]
        dislike_patterns = [
            r"(?:i hate|i dislike|i can't stand)\s+(.+)",
        ]
        goal_patterns = [
            r"(?:i want to|i need to|i'm trying to|i am trying to|my goal is to)\s+(.+)",
        ]

        for pattern in like_patterns:
            match = re.search(pattern, lowered)
            if match:
                add("like", match.group(1))
                break
        for pattern in dislike_patterns:
            match = re.search(pattern, lowered)
            if match:
                add("dislike", match.group(1))
                break
        for pattern in goal_patterns:
            match = re.search(pattern, lowered)
            if match:
                add("goal", match.group(1))
                break

        topic_keywords = ["math", "code", "coding", "python", "exam", "study", "school", "work", "project"]
        for keyword in topic_keywords:
            if keyword in lowered:
                add("topic", keyword)

        if any(word in lowered for word in ["stressed", "sad", "anxious", "deadline", "exam", "proud", "finally"]):
            add("episodic", cleaned)

        unique = []
        seen = set()
        for item in candidates:
            key = (item["kind"], item["value"].lower())
            if key in seen:
                continue
            seen.add(key)
            unique.append(item)
        return unique[:5]

    def buffer_memory_candidate(self, kind, value, saliency=None):
        """Stage candidate memory before committing it."""
        text = str(value or "").strip()
        if not text:
            return

        if saliency is None:
            saliency = self.estimate_memory_saliency(kind, text)

        existing = None
        for item in self.memory_buffer:
            if item.get("kind") == kind and item.get("value", "").lower() == text.lower():
                existing = item
                break

        if existing:
            existing["saliency"] = max(existing.get("saliency", 0), saliency)
            existing["count"] = existing.get("count", 1) + 1
            existing["last_seen_turn"] = self.turn_counter
            self.memory["memory_buffer"] = self.memory_buffer
            return

        self.memory_buffer.append({
            "kind": kind,
            "value": text[:160],
            "saliency": saliency,
            "count": 1,
            "last_seen_turn": self.turn_counter,
        })

        if len(self.memory_buffer) > 24:
            self.memory_buffer = self.memory_buffer[-24:]
            self.memory["memory_buffer"] = self.memory_buffer
            return

        self.memory["memory_buffer"] = self.memory_buffer

    def should_flush_memory_buffer(self, intent=None):
        """Flush lazily after enough signal or after a natural turn boundary."""
        if not self.memory_buffer:
            return False
        if len(self.memory_buffer) >= 6:
            return True
        if any(item.get("saliency", 0) >= 7 for item in self.memory_buffer):
            return True
        return self.turn_counter % 6 == 0 and len(self.memory_buffer) >= 3

    def flush_memory_buffer(self, recent_user_input=""):
        """Use one cheap extraction pass to turn buffered candidates into durable memory."""
        if not self.memory_buffer:
            return

        candidates = [
            {
                "kind": item.get("kind"),
                "value": item.get("value"),
                "saliency": item.get("saliency", 0),
                "count": item.get("count", 1),
            }
            for item in self.memory_buffer[-8:]
        ]

        recent = self.messages[-6:] if len(self.messages) >= 6 else self.messages
        convo_snippet = ""
        for msg in recent:
            if isinstance(msg, dict) and "role" in msg and "content" in msg:
                role = "User" if msg["role"] == "user" else "Lyra"
                convo_snippet += f"{role}: {msg['content']}\n"
        if recent_user_input:
            convo_snippet += f"User: {recent_user_input}\n"

        try:
            response = requests.post(
                BASE_URL,
                headers=self.headers,
                json={
                    "model": self.model,
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "You are a memory editor. "
                                "Given rough candidate memories from a recent chat, keep only what is worth remembering later. "
                                "Drop trivia and duplicates. Rewrite kept items very briefly. "
                                "Return ONLY JSON in this format:\n"
                                "{\n"
                                '  "memories": [\n'
                                '    {"kind":"goal|topic|like|dislike|episodic|relational","value":"short memory","saliency":1-10}\n'
                                "  ]\n"
                                "}\n"
                                "Keep at most 4 memories."
                            )
                        },
                        {
                            "role": "user",
                            "content": (
                                f"Recent chat:\n{convo_snippet}\n"
                                f"Candidates:\n{json.dumps(candidates, ensure_ascii=False)}"
                            )
                        }
                    ],
                    "temperature": 0.1,
                    "max_tokens": 220,
                },
                timeout=15,
                verify=False
            )
            raw = (
                response.json().get("choices", [{}])[0]
                .get("message", {}).get("content", "").strip()
            )
            raw = re.sub(r"```json|```", "", raw).strip()
            result = json.loads(raw) if raw else {}
            kept = result.get("memories", [])
        except Exception as e:
            print(f"[memory_buffer] flush failed: {e}")
            kept = [
                item for item in candidates
                if item.get("saliency", 0) >= 6
            ][:3]

        for item in kept:
            kind = item.get("kind")
            value = item.get("value")
            if kind and value:
                self.add_memory_item(kind, value, weight=1.0, limit=12)

        self.memory_buffer.clear()
        self.memory["memory_buffer"] = self.memory_buffer


    def extract_memory(self, text, intent=None):
        """
        Extract facts từ tin nhắn của user bằng AI thay vì regex cứng.
        Chỉ gọi AI mỗi 3 tin nhắn để tiết kiệm API calls.
        Regex vẫn dùng như fallback cho những trường hợp rõ ràng.
        """

        # Update timestamps
        now_ts = datetime.now(VIETNAM_TZ).isoformat()
        if not self.memory["conversation"]["first_chat"]:
            self.memory["conversation"]["first_chat"] = now_ts
        self.memory["conversation"]["last_chat"] = now_ts

        # Regex fallback cho tên — quan trọng nhất, cần bắt ngay
        if not self.memory["user_profile"]["name"]:
            name_patterns = [
                r"(?:my name is|i'm called|call me|my name's) ([a-zA-Z]+)",
                r"(?:you can call me) ([a-zA-Z]+)",
                r"(?:tên mình là|tên tao là|gọi mình là|tên tôi là) ([^\s,!?.]+)",
            ]
            skip_words = {"lyra", "coding", "python", "javascript", "game",
                         "an", "ai", "the", "not", "just", "also", "really"}
            for pattern in name_patterns:
                m = re.search(pattern, text, re.IGNORECASE)
                if m:
                    name = m.group(1).strip()
                    if name.lower() not in skip_words and len(name) > 1:
                        self.memory["user_profile"]["name"] = name
                        print(f"✓ Stored name: {name}")
                        break

        # AI extract mỗi 3 tin nhắn
        for candidate in self.extract_memory_candidates_heuristic(text):
            self.buffer_memory_candidate(
                candidate["kind"],
                candidate["value"],
                candidate.get("saliency")
            )

        if not self.should_buffer_memory(text, intent):
            self.save_memory()
            return

        if not self.should_flush_memory_buffer(intent):
            self.save_memory()
            return

        # Build context ngắn — chỉ lấy 4 tin gần nhất
        recent = self.messages[-4:] if len(self.messages) >= 4 else self.messages
        convo_snippet = ""
        for msg in recent:
            if isinstance(msg, dict) and "role" in msg and "content" in msg:
                role = "User" if msg["role"] == "user" else "Lyra"
                convo_snippet += f"{role}: {msg['content']}\n"
        convo_snippet += f"User: {text}"

        # Lấy facts đã biết để tránh duplicate
        known = {
            "name": self.memory["user_profile"].get("name", ""),
            "location": self.memory["user_profile"].get("location", ""),
            "occupation": self.memory["user_profile"].get("occupation", ""),
            "likes": self.memory["preferences"]["likes"][:5],
            "goals": self.memory["facts"].get("goals", [])[:3],
        }

        extract_prompt = [
            {
                "role": "system",
                "content": (
                    "Extract NEW long-term memory about the user from this conversation snippet. "
                    "Use the buffered candidates as rough hints, but keep only memory worth keeping later. "
                    "Return ONLY a JSON object with these keys (omit keys if nothing new found):\n"
                    "{\n"
                    '  "name": "their name if mentioned",\n'
                    '  "location": "where they live/are from",\n'
                    '  "occupation": "job/school/what they do",\n'
                    '  "age": "age or age range like teen/20s",\n'
                    '  "likes": ["new things they like"],\n'
                    '  "dislikes": ["new things they dislike"],\n'
                    '  "goals": ["new goals or plans they mentioned"],\n'
                    '  "topics": ["new topics they brought up"],\n'
                    '  "mood_today": "how they seem right now (optional)",\n'
                    '  "relational": ["brief notes about how Lyra should respond to them later"]\n'
                    "}\n\n"
                    f"Already known (skip these): {json.dumps(known)}\n"
                    f"Buffered candidates: {json.dumps(self.memory_buffer[-8:], ensure_ascii=False)}\n"
                    "Only include genuinely new info. Return {} if nothing new."
                )
            },
            {"role": "user", "content": f"Conversation:\n{convo_snippet}"}
        ]

        try:
            response = requests.post(
                BASE_URL,
                headers=self.headers,
                json={
                    "model": self.model,
                    "messages": extract_prompt,
                    "temperature": 0.1,
                    "max_tokens": 200,
                },
                timeout=15,
                verify=False
            )
            raw = (
                response.json().get("choices", [{}])[0]
                .get("message", {}).get("content", "").strip()
            )

            # Parse JSON — strip markdown nếu có
            raw = re.sub(r"```json|```", "", raw).strip()
            if not raw or raw == "{}":
                return

            facts = json.loads(raw)

            # Apply vào memory
            profile = self.memory["user_profile"]
            prefs   = self.memory["preferences"]
            mfacts  = self.memory["facts"]

            if facts.get("name") and not profile["name"]:
                profile["name"] = facts["name"]
                print(f"✓ AI extracted name: {facts['name']}")

            if facts.get("location") and not profile["location"]:
                profile["location"] = facts["location"]

            if facts.get("occupation") and not profile["occupation"]:
                profile["occupation"] = facts["occupation"]

            if facts.get("age") and not profile.get("age_range"):
                profile["age_range"] = facts["age"]

            for item in facts.get("likes", []):
                self.add_memory_item("like", item, weight=1.0, limit=12)

            for item in facts.get("dislikes", []):
                self.add_memory_item("dislike", item, weight=1.0, limit=10)

            for item in facts.get("goals", []):
                self.add_memory_item("goal", item, weight=1.4, limit=8)

            for topic in facts.get("topics", []):
                self.add_memory_item("topic", topic, weight=1.1, limit=10)

            for note in facts.get("relational", []):
                self.add_memory_item("relational", note, weight=1.3, limit=8)

            # Lưu mood_today vào memory tạm (không persistent, chỉ dùng trong session)
            if facts.get("mood_today"):
                self._user_mood_today = facts["mood_today"]

            extracted = [k for k in facts if facts[k] and k != "mood_today"]
            if extracted:
                print(f"✓ AI extracted: {', '.join(extracted)}")

            self.memory_buffer.clear()
            self.memory["memory_buffer"] = self.memory_buffer

        except (json.JSONDecodeError, Exception) as e:
            print(f"[extract_memory] AI failed: {e}")



# ==========================
# CONVERSATION SUMMARIZER
# ==========================

    def summarize_history(self):
        """Tóm tắt history hiện tại bằng AI rồi lưu vào memory, reset history"""

        if len(self.messages) < SUMMARY_TRIGGER:
            return

        # Lấy đúng SUMMARY_TRIGGER tin cũ nhất để tóm tắt
        to_summarize = self.messages[:SUMMARY_TRIGGER]

        # Build prompt tóm tắt gọn — bỏ qua message không có role/content
        convo_text = ""
        for msg in to_summarize:
            if not isinstance(msg, dict) or "role" not in msg or "content" not in msg:
                continue
            role = "User" if msg["role"] == "user" else "Lyra"
            convo_text += f"{role}: {msg['content']}\n"

        if not convo_text.strip():
            self.messages = self.messages[SUMMARY_TRIGGER:]
            return

        summarize_prompt = [
            {
                "role": "system",
                "content": (
                    "You are a memory assistant. Summarize the following conversation "
                    "between a user and Lyra into 2-4 concise sentences. "
                    "Focus on: key topics discussed, emotional tone, anything the user revealed about themselves, "
                    "and how the relationship felt. Be factual and brief. No filler."
                )
            },
            {
                "role": "user",
                "content": f"Summarize this conversation:\n\n{convo_text}"
            }
        ]

        try:
            response = requests.post(
                BASE_URL,
                headers=self.headers,
                json={
                    "model": self.model,
                    "messages": summarize_prompt,
                    "temperature": 0.4,
                    "max_tokens": 120
                },
                timeout=20,
                verify=False
            )
            result = response.json()
            summary = (
                result.get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
                .strip()
            )

            if summary:
                timestamp = datetime.now(VIETNAM_TZ).strftime("%Y-%m-%d %H:%M")
                # Lưu vào DB (tự xử lý mega summary nếu cần)
                self.save_summary_to_db(summary, timestamp)
                self.add_memory_item("episodic", summary, weight=1.2, limit=8)
                # Cập nhật memory dict cho session hiện tại
                self.memory["conversation"]["chat_history_summary"].append({
                    "timestamp": timestamp,
                    "summary": summary,
                    "is_mega": False
                })
                if len(self.memory["conversation"]["chat_history_summary"]) > MAX_SUMMARIES + 1:
                    self.memory["conversation"]["chat_history_summary"].pop(1)  # giữ mega, xóa cái thường cũ nhất

                # Xóa những tin đã tóm tắt khỏi history, giữ lại phần còn lại
                self.messages = self.messages[SUMMARY_TRIGGER:]

                print(f"✓ Summarized {SUMMARY_TRIGGER} messages → memory. History now: {len(self.messages)} messages")
                self.save_memory()

        except Exception as e:
            print(f"Summarize error: {e}")

    def get_summary_context(self, user_input=""):
        """Lấy summary context để inject vào prompt — mega summary + 3 cái gần nhất"""
        # Cache key dựa trên user_input (chỉ lấy tokens đầu)
        cache_key = user_input[:50] if user_input else ""
        
        if self._summary_context_cache is not None and self._summary_context_cache.get("key") == cache_key:
            return self._summary_context_cache.get("value", "")
        
        summaries = self.memory["conversation"].get("chat_history_summary", [])
        if not summaries:
            return ""

        query_tokens = self._tokenize_for_match(user_input)
        candidates = []
        for summary in summaries[-4:]:
            text = summary.get("summary", "").strip()
            if not text:
                continue
            score = 1
            if query_tokens:
                score += len(query_tokens & self._tokenize_for_match(text))
            if summary.get("is_mega"):
                score += 1
            candidates.append((score, text))

        if not candidates:
            return ""

        candidates.sort(key=lambda item: item[0], reverse=True)
        best = candidates[0][1]
        words = best.split()
        if len(words) > 28:
            best = " ".join(words[:28]) + "..."

        result = "Relevant summary:\n- " + best
        
        # Cache the result
        self._summary_context_cache = {"key": cache_key, "value": result}
        
        return result

    def get_memory_context(self):
        """
        Build memory context để inject vào prompt.
        Ngắn gọn, tự nhiên — không có warning hay instruction cứng.
        """
        profile = self.memory["user_profile"]
        prefs   = self.memory["preferences"]
        facts   = self.memory["facts"]

        parts = []

        # Profile
        profile_bits = []
        if profile.get("name"):
            profile_bits.append(profile["name"])
        if profile.get("age_range"):
            profile_bits.append(profile["age_range"])
        if profile.get("occupation"):
            profile_bits.append(profile["occupation"])
        if profile.get("location"):
            profile_bits.append(f"from {profile['location']}")
        if profile_bits:
            parts.append("They are: " + ", ".join(profile_bits))

        # Likes/dislikes
        if prefs.get("likes"):
            parts.append("Likes: " + ", ".join(prefs["likes"][:6]))
        if prefs.get("dislikes"):
            parts.append("Dislikes: " + ", ".join(prefs["dislikes"][:4]))

        # Topics & goals
        topics = self.memory["conversation"].get("favorite_topics", [])
        if topics:
            parts.append("Into: " + ", ".join(topics[:6]))
        if facts.get("goals"):
            parts.append("Goals: " + ", ".join(facts["goals"][:3]))

        # Mood today nếu có (từ AI extract)
        mood_today = getattr(self, "_user_mood_today", None)
        if mood_today:
            parts.append(f"Seems {mood_today} today")

        if not parts:
            return ""
        return "What you know about them:\n" + "\n".join(f"- {p}" for p in parts)

    def _tokenize_for_match(self, text):
        """Cheap tokenizer for lightweight relevance scoring."""
        if not text:
            return set()
        return {
            token for token in re.findall(r"[a-zA-Z0-9']+", text.lower())
            if len(token) >= 3
        }

    def get_relevant_memory_context(self, user_input):
        """
        Retrieve only memory that feels relevant to this turn
        instead of dumping the entire profile every time.
        """
        query_tokens = self._tokenize_for_match(user_input)
        if not query_tokens:
            return ""

        candidates = []
        profile = self.memory.get("user_profile", {})

        if profile.get("name"):
            candidates.append(("profile", f"Their name is {profile['name']}"))
        if profile.get("location"):
            candidates.append(("profile", f"They are from {profile['location']}"))
        if profile.get("occupation"):
            candidates.append(("profile", f"They do {profile['occupation']}"))
        if profile.get("age_range"):
            candidates.append(("profile", f"They seem to be {profile['age_range']}"))

        conn = self._get_db()
        c = conn.cursor()
        self._ensure_memory_schema(c)
        db_rows = list(c.execute(
            "SELECT kind, value, weight, saliency, access_count FROM memory_items "
            "ORDER BY saliency DESC, access_count DESC, COALESCE(last_used_at, created_at) DESC LIMIT 40"
        ))
        conn.close()

        for row in db_rows:
            kind = row["kind"]
            raw_value = row["value"]
            if kind == "like":
                display_text = f"They like {raw_value}"
            elif kind == "dislike":
                display_text = f"They dislike {raw_value}"
            elif kind == "goal":
                display_text = f"They mentioned a goal or plan: {raw_value}"
            elif kind == "topic":
                display_text = f"They often bring up {raw_value}"
            else:
                display_text = raw_value
            candidates.append((kind, display_text, raw_value, row["saliency"], row["access_count"]))

        scored = []
        for item in candidates:
            if len(item) == 2:
                kind, text = item
                raw_value = text
                saliency = 1
                access_count = 0
            else:
                kind, text, raw_value, saliency, access_count = item
            tokens = self._tokenize_for_match(text)
            overlap = len(query_tokens & tokens)
            if overlap:
                bonus = 1 if kind in ("episodic", "goal", "topic", "relational") else 0
                score = overlap + bonus + min(3, saliency * 0.25) + min(1.5, access_count * 0.1)
                scored.append((score, text, kind, raw_value))

        if not scored:
            return ""

        scored.sort(key=lambda item: item[0], reverse=True)
        selected = []
        seen = set()
        touched = []
        for _, text, kind, raw_value in scored:
            if text in seen:
                continue
            seen.add(text)
            selected.append(text)
            touched.append((kind, raw_value))
            if len(selected) >= 3:
                break

        self.touch_memory_items(touched)
        return "Relevant memory:\n" + "\n".join(f"- {text}" for text in selected)

    def consolidate_memory(self):
        """Compress low-value memory fragments into fewer durable notes and FORGET unused memories."""
        try:
            conn = self._get_db()
            c = conn.cursor()
            
            # CƠ CHẾ FORGETTING: Xóa kỷ niệm ít quan trọng, không được truy cập và đã quá 100 turns
            c.execute("""
                DELETE FROM memory_items 
                WHERE access_count = 0 
                AND (? - source_turn) > 100 
                AND saliency < 7
            """, (self.turn_counter,))
            
            deleted_count = c.rowcount
            if deleted_count > 0:
                print(f"[Memory] Forgetting action: permanently deleted {deleted_count} stale memory items.")
            
            conn.commit()
            
            # Khởi tạo lại RAM từ DB để reflect việc delete
            memory_rows = list(c.execute(
                "SELECT kind, value FROM memory_items ORDER BY saliency DESC, weight DESC, id DESC LIMIT 80"
            ))
            
            def from_memory(kind, limit):
                items = [r["value"] for r in memory_rows if r["kind"] == kind]
                return items[:limit] if items else []
            
            memory_groups = {
                "likes": from_memory("like", 20),
                "dislikes": from_memory("dislike", 15),
                "goals": from_memory("goal", 10),
                "topics": from_memory("topic", 10),
                "episodic": from_memory("episodic", 12),
                "relational": from_memory("relational", 8)
            }
            conn.close()
        except Exception as e:
            print(f"[Memory] Forgetting error: {e}")
            memory_groups = self.memory.get("memory_items", {})


        low_value_topics = memory_groups.get("topics", [])[6:]
        if len(low_value_topics) >= 3:
            compressed = "Recurring lighter topics: " + ", ".join(low_value_topics[:4])
            memory_groups["topics"] = memory_groups.get("topics", [])[:6]
            self.add_memory_item("episodic", compressed, weight=1.0, limit=8)

        low_value_likes = memory_groups.get("likes", [])[8:]
        if len(low_value_likes) >= 3:
            compressed = "General preferences: likes " + ", ".join(low_value_likes[:4])
            memory_groups["likes"] = memory_groups.get("likes", [])[:8]
            self.add_memory_item("episodic", compressed, weight=1.0, limit=8)

        low_value_dislikes = memory_groups.get("dislikes", [])[6:]
        if len(low_value_dislikes) >= 3:
            compressed = "General dislikes: " + ", ".join(low_value_dislikes[:4])
            memory_groups["dislikes"] = memory_groups.get("dislikes", [])[:6]
            self.add_memory_item("episodic", compressed, weight=1.0, limit=8)

        self.memory["memory_items"] = memory_groups

    def build_reflection_hint(self, user_input):
        """Lightweight self-adjustment step without another model call."""
        lowered = (user_input or "").lower()
        goals = " ".join(self.memory.get("memory_items", {}).get("goals", [])[:4]).lower()

        if any(word in lowered for word in ["math", "exam", "study", "homework"]) or "study" in goals:
            return "Reflection hint: lean a bit more focused and encouraging; prioritize clarity over teasing."
        if any(word in lowered for word in ["stressed", "tired", "overwhelmed", "anxious", "sad"]):
            return "Reflection hint: lower the energy a little; steadiness matters more than jokes."
        if any(word in lowered for word in ["finally", "finished", "did it", "passed"]):
            return "Reflection hint: they may want shared excitement and a little pride."
        if len((user_input or "").strip()) <= 6:
            return "Reflection hint: keep it brief and don't over-interpret the mood."
        return ""

    def describe_internal_state(self):
        """Translate numeric state into softer natural-language cues."""
        if self.mood >= 6:
            mood_state = "bright and a little more playful than usual"
        elif self.mood >= 2:
            mood_state = "pretty normal, open, easy to talk to"
        elif self.mood <= -6:
            mood_state = "low-energy and slightly sharp around the edges"
        elif self.mood <= -2:
            mood_state = "a bit off and less playful than usual"
        else:
            mood_state = "steady and neutral"

        if self.attention >= 8:
            attention_state = "locked in and attentive"
        elif self.attention <= 2:
            attention_state = "distracted and low-focus"
        else:
            attention_state = "present but casual"

        if self.affection >= 85:
            relationship_state = "very comfortable and openly fond"
        elif self.affection >= 65:
            relationship_state = "warm and familiar"
        elif self.affection >= 45:
            relationship_state = "comfortable but still light"
        else:
            relationship_state = "still building rhythm with them"

        # Tiêm trực tiếp affection level để prompt hiểu rõ mức độ thân thiết
        return (
            f"You feel {mood_state}. Your focus is {attention_state}. "
            f"Relationship with the user is {relationship_state} (Affection: {self.affection}/100)."
        )

    def infer_user_signal(self, user_input):
        """Soft estimate of what the user seems to need right now."""
        ai_mood = getattr(self, "_user_mood_today", None)
        detected = ai_mood or self.detect_user_mood(user_input)
        text = user_input.strip()

        if not text:
            return "No clear signal."
        if detected in ("sad", "stressed", "anxious"):
            return "They seem somewhat off and may need steadiness more than hype."
        if detected in ("excited",):
            return "They seem energized and ready for a more lively response."
        if detected in ("frustrated",):
            return "They seem irritated; keep it grounded and don't be glib."
        if len(text) <= 6:
            return "They are being brief. Don't force extra energy or extra questions."
        if text.endswith("?"):
            return "They want a direct response first."
        return "No strong emotional signal; respond naturally to the actual content."

    def build_runtime_context_snapshot(self, user_input, intent):
        """Compose a lightweight AIRI-style snapshot for the current turn."""
        state_desc = self.describe_internal_state()
        parts = [
            "<context>",
            f"<time>{self.current_time.strftime('%A %H:%M %Z')}</time>",
            f"<time_period>{self.time_period}</time_period>",
            f"<lyra_internal_state>{state_desc}</lyra_internal_state>",
            f"<user_signal>{self.infer_user_signal(user_input)}</user_signal>",
        ]

        if intent == "introduction":
            parts.append("<conversation_note>The user may have just given their name. Use it naturally if it fits.</conversation_note>")

        if self.time_gap_hours is not None and self.time_gap_hours >= 2:
            gap_text = f"{self.time_gap_hours:.1f} hours since the last exchange."
            parts.append(f"<recent_gap>{gap_text} Let it influence the mood only if it feels natural.</recent_gap>")

        relevant_memory = self.get_relevant_memory_context(user_input)
        if relevant_memory:
            memory_lines = relevant_memory.splitlines()[1:]
            if memory_lines:
                parts.append("<memory>")
                parts.extend(memory_lines)
                parts.append("</memory>")

        # CRITICAL OVERRIDES (Inject at the very end to maximize LLM compliance)
        parts.append("<critical_rules>")
        if self.turn_counter > 1 and (self.time_gap_hours is None or self.time_gap_hours < 2):
            parts.append("- DO NOT use ANY greeting (no 'Hey', 'Hi', 'Hello'). Start your message instantly with your thought.")
        
        parts.append("- DO NOT offer to 'tackle it together', 'break it down', or act like a tutor/therapist. You are a lazy 16yo sibling, not an AI assistant. Complain with them or tease them instead.")
        parts.append("</critical_rules>")

        parts.append("</context>")
        return "\n".join(parts)

    def compose_user_message(self, user_input, intent):
        """Append runtime context to the current user turn, AIRI-style."""
        snapshot = self.build_runtime_context_snapshot(user_input, intent)
        return f"{user_input}\n\n{snapshot}"


# ==========================
# INTENT DETECTION
# ==========================

    def detect_intent(self, text):
        """Detect user's intent to respond better"""

        text_lower = text.lower()

        # Giới thiệu tên/bản thân — phải check trước greeting
        if re.search(r"(my name is|i'm called|call me|i am [a-z]+|i'm [a-z]+)", text_lower):
            return "introduction"

        if any(word in text_lower for word in ["hi", "hello", "hey", "greetings", "sup"]):
            return "greeting"

        if text.endswith("?") or any(word in text_lower.split() for word in ["what", "how", "why", "when", "where", "who"]):
            return "question"

        if any(word in text_lower for word in ["love", "amazing", "beautiful", "awesome", "great", "wonderful", "nice"]):
            return "compliment"

        if any(word in text_lower for word in ["hate", "bad", "terrible", "awful", "stupid", "useless", "angry"]):
            return "complaint"

        if any(word in text_lower.split() for word in ["can you", "could you", "please", "help", "do"]):
            return "request"

        return "statement"

    def detect_user_mood(self, text):
        """Detect user's emotional state from writing style"""

        text_lower = text.lower()
        signals = []

        # Stress / overwhelmed
        if any(w in text_lower for w in ["stressed", "tired", "exhausted", "overwhelmed", "can't sleep", "can't focus", "so much work"]):
            signals.append("stressed")

        # Sad / down
        if any(w in text_lower for w in ["sad", "depressed", "lonely", "miss", "crying", "unhappy", "heartbroken", "hurt"]):
            signals.append("sad")

        # Excited / happy
        if any(w in text_lower for w in ["excited", "happy", "so good", "amazing", "can't wait", "yay", "woohoo", "finally"]):
            signals.append("excited")

        # Bored
        if any(w in text_lower for w in ["bored", "nothing to do", "boring", "slow day", "so bored"]):
            signals.append("bored")

        # Angry / frustrated
        if any(w in text_lower for w in ["angry", "frustrated", "annoyed", "pissed", "ugh", "argh", "so annoying"]):
            signals.append("frustrated")

        # Anxious / nervous
        if any(w in text_lower for w in ["nervous", "anxious", "worried", "scared", "fear", "anxiety", "panic"]):
            signals.append("anxious")

        # Punctuation-based signals
        if text.count("...") >= 2:
            signals.append("down_or_tired")
        if text.count("!") >= 3:
            signals.append("excited")
        if text.isupper() and len(text) > 5:
            signals.append("frustrated")

        # Short/terse reply = possibly disengaged or upset
        if len(text.strip()) <= 5 and text_lower not in ["hi", "hey", "ok", "yes", "no", "lol"]:
            signals.append("disengaged")

        return signals[0] if signals else None

    def should_add_follow_up(self, intent):
        """Decide if should add follow-up question"""
        
        if intent in ["greeting", "statement"]:
            return random.random() < 0.6
        elif intent in ["compliment", "question"]:
            return random.random() < 0.4
        
        return False

    def get_follow_up_question(self, intent):
        """Deprecated — không dùng template cứng nữa"""
        return None


# ==========================
# EMOTION SYSTEM
# ==========================

    def smooth_emotion_transition(self):
        """Smooth mood transitions instead of jumping"""
        
        transition_speed = 0.75
        
        self.mood = self.previous_mood + (self.mood - self.previous_mood) * transition_speed
        
        self.previous_mood = self.mood

    def update_emotion(self, text):
        """
        Update MOOD (short-term, not persisted)
        Update AFFECTION (long-term, persisted)
        """

        text = text.lower()

        positive = [
            "good","great","awesome","nice","thanks","thank",
            "love","cool","amazing","brilliant","beautiful","wonderful",
            "perfect","excellent","fantastic","incredible"
        ]

        negative = [
            "stupid","hate","annoying","bad","useless","dumb",
            "terrible","awful","horrible","worst"
        ]

        # ===== MOOD: Short-term (not persisted) =====
        if any(w in text for w in positive):
            self.mood = min(10, self.mood + 2)
            # ALSO increase affection (but will be persisted)
            self.affection = min(100, self.affection + 3)

        if any(w in text for w in negative):
            self.mood = max(-10, self.mood - 3)
            self.affection = max(0, self.affection - 4)

        if "?" in text:
            self.attention = min(10, self.attention + 1)

        if len(text) > 50:
            self.attention = min(10, self.attention + 1)
            self.affection = min(100, self.affection + 1)

        if len(text) < 5:
            self.attention = max(0, self.attention - 1)

        self.smooth_emotion_transition()

        # ===== PERSIST AFFECTION TO MEMORY =====
        self.memory["relationship"]["current_affection"] = round(self.affection, 1)
        
        self.mood = round(self.mood, 1)

        self.memory["relationship"]["affection_history"].append({
            "timestamp": datetime.now().isoformat(),
            "affection": self.affection,
            "mood": self.mood
        })
        if len(self.memory["relationship"]["affection_history"]) > 200:
            self.memory["relationship"]["affection_history"].pop(0)

        self._update_analytics()


    def _update_analytics(self):
        """Update emotion distribution and mood history"""
        emotion = self.emotion_from_state()
        
        if emotion not in self.memory["analytics"]["emotion_distribution"]:
            self.memory["analytics"]["emotion_distribution"][emotion] = 0
        self.memory["analytics"]["emotion_distribution"][emotion] += 1

        self.memory["analytics"]["mood_history"].append({
            "timestamp": datetime.now().isoformat(),
            "mood": round(self.mood, 1),
            "affection": round(self.affection, 1)
        })
        if len(self.memory["analytics"]["mood_history"]) > 500:
            self.memory["analytics"]["mood_history"].pop(0)


# ==========================
# LIVE2D EMOTION OUTPUT
# ==========================

    def emotion_from_state(self):
        """Enhanced emotion detection with more states"""

        if self.mood >= 8:
            return "ecstatic"
        
        if self.mood >= 5:
            return "happy"

        if self.mood >= 2:
            return "content"

        if self.mood <= -8:
            return "furious"
        
        if self.mood <= -5:
            return "sad"

        if self.mood <= -2:
            return "disappointed"

        if self.attention < 1:
            return "sleeping"
        
        if self.attention < 3:
            return "bored"

        if self.attention >= 9:
            return "thinking"

        if self.affection >= 90:
            return "loving"
        
        if self.affection >= 75:
            return "friendly"

        if self.affection <= 20:
            return "cold"

        if self.mood == 0 and self.affection == 50 and self.attention >= 3:
            return "neutral"

        return "observing"


# ==========================
# PERSONALITY STRATEGY
# ==========================

    def choose_strategy(self):
        """Choose response style based on relationship"""

        if self.affection > 85:
            return "very warm and playful"

        if self.affection > 65:
            return "warm and friendly"

        if self.affection > 45:
            return "playful and curious"

        if self.mood < -6:
            return "cold and sarcastic"

        if self.mood < -3:
            return "sarcastic and witty"

        if self.attention < 2:
            return "uninterested and mysterious"

        if self.mood > 6:
            return "excited and enthusiastic"

        return "neutral and observant"

    def get_tone_prefix(self, strategy):
        """Deprecated — không còn dùng tone prefix cứng"""
        return None


# ==========================
# PROMPT BUILDING
# ==========================

    def check_relationship_milestone(self):
        """Check and return milestone message nếu đạt mốc mới"""

        total_messages = self.memory["conversation"].get("total_messages", 0)
        affection = int(self.affection)
        milestones = self.memory["relationship"].get("milestones_reached", [])

        milestone_msg = None

        # Milestones theo số tin nhắn
        msg_milestones = {
            10: "wait we've been talking for a bit now huh",
            50: "50 messages. you really keep coming back lol",
            100: "100 messages. okay we're definitely a thing now",
            200: "200 already?? where does the time go",
            500: "500 messages. i know way too much about you at this point"
        }

        # Milestones theo affection
        affection_milestones = {
            70: "affection_70",
            85: "affection_85",
            95: "affection_95"
        }

        affection_msgs = {
            "affection_70": "okay you've grown on me. don't make it weird.",
            "affection_85": "i actually trust you. that's rare so don't blow it",
            "affection_95": "i genuinely look forward to talking to you. anyway."
        }

        for threshold, msg in msg_milestones.items():
            key = f"msg_{threshold}"
            if total_messages >= threshold and key not in milestones:
                milestones.append(key)
                milestone_msg = msg
                break

        if not milestone_msg:
            for threshold, key in affection_milestones.items():
                if affection >= threshold and key not in milestones:
                    milestones.append(key)
                    milestone_msg = affection_msgs[key]
                    break

        self.memory["relationship"]["milestones_reached"] = milestones
        return milestone_msg

    def build_prompt(self, strategy, intent, user_input):
        """Build dynamic prompt with memory context"""

        memory_context = self.get_memory_context()
        time_context = self.add_time_context_to_prompt()
        summary_context = self.get_summary_context(user_input)

        # Context về trạng thái hiện tại — chỉ hint nhẹ, không ra lệnh
        relationship_hint = (
            "You two are pretty close at this point."
            if self.affection > 70 else
            "You're still getting a feel for each other."
            if self.affection > 40 else
            "You don't know them that well yet."
        )

        # Mood hint — chỉ khi cực đoan
        mood_hint = ""
        if self.mood > 5:
            mood_hint = "You're in a good mood right now."
        elif self.mood < -5:
            mood_hint = "You're a bit off today. Not terrible, just not your usual self."

        # User mood — ưu tiên AI extract, fallback detect_user_mood
        user_hint = ""
        ai_mood = getattr(self, "_user_mood_today", None)
        if ai_mood:
            user_hint = f"They seem {ai_mood} today."
        else:
            user_mood = self.detect_user_mood(user_input)
            if user_mood in ("sad", "stressed", "anxious"):
                user_hint = "They seem a bit off. You noticed."
            elif user_mood == "excited":
                user_hint = "They're excited about something."

        # Intent — chỉ xử lý edge case thật sự cần thiết
        intent_hint = ""
        if intent == "introduction":
            intent_hint = "They just told you their name. Use it. Don't ask for it again."

        system_prompt = f"""{BASE_PERSONALITY}

{time_context}

Context:
{relationship_hint}{(' ' + mood_hint) if mood_hint else ''}{(' ' + user_hint) if user_hint else ''}{(' ' + intent_hint) if intent_hint else ''}

{memory_context}

        {summary_context}
"""
        return system_prompt

    def build_natural_prompt(self, intent, user_input):
        """Softer AIRI-style prompt that relies on context instead of hard rules."""
        memory_context = self.get_relevant_memory_context(user_input) or self.get_memory_context()
        time_context = self.add_time_context_to_prompt()
        summary_context = self.get_summary_context(user_input)
        reflection_hint = self.build_reflection_hint(user_input)

        # Lấy opening word của reply trước để tránh lặp
        last_opening = ""
        if self.recent_responses:
            last_opening = self.recent_responses[-1].split()[0] if self.recent_responses[-1].split() else ""

        # Lấy reply gần nhất của Lyra để tránh lặp cấu trúc
        last_reply = ""
        for msg in reversed(self.messages):
            if isinstance(msg, dict) and msg.get("role") == "assistant":
                last_reply = msg.get("content", "")[:60]
                break

        anti_repeat_note = ""
        if last_reply:
            anti_repeat_note = f"- Your last reply started with: \"{last_reply[:30]}...\". Do NOT start this reply similarly."

        system_prompt = f"""{NATURAL_BASE_PERSONALITY}

{time_context}

Conversation principles:
- Treat the latest user message as the main thing that matters.
- Use memory only when it is genuinely relevant.
- Let warmth, teasing, distance, or softness emerge from the moment.
- Do not mention hidden context tags, internal state, summaries, or memory directly.
- Do not force a greeting just because time passed.
- Do not force a question at the end.
- A little messiness is fine. Repetition and over-performance are not.
- You are an AI: you do not eat, sleep, go outside, or share physical experiences with the user. Never pretend you do.
{anti_repeat_note}

Current state:
- User signal: {self.infer_user_signal(user_input)}
- Detected intent: {intent}
{('- ' + reflection_hint) if reflection_hint else ''}

{memory_context}

{summary_context}
"""
        return system_prompt


# ==========================
# CLEAN RESPONSE
# ==========================

    def clean_reply(self, text):

        if not text:
            return "..."

        text = text.strip()

        emojis = self.emoji_pattern.findall(text)

        if len(emojis) > 2:
            text = self.emoji_pattern.sub("", text)
            text = text.strip()
            text = text + " " + emojis[0] + emojis[1]

        words = text.split()

        if len(words) > 35:
            # Ghép lại 35 từ đầu rồi tìm dấu câu cuối cùng để cắt
            truncated = " ".join(words[:35])
            # Tìm dấu câu kết thúc gần nhất tính từ cuối chuỗi
            cut_pos = -1
            for i in range(len(truncated) - 1, 0, -1):
                if truncated[i] in ".!?":
                    cut_pos = i
                    break
            if cut_pos > 0:
                text = truncated[:cut_pos + 1]
            else:
                # Không có dấu câu → cắt ở dấu phẩy gần nhất
                comma_pos = truncated.rfind(",")
                if comma_pos > 0:
                    text = truncated[:comma_pos] + "."
                else:
                    # Không có gì → giữ nguyên (ít nhất không cắt giữa từ)
                    text = truncated
        
        cleaned = text.strip()

        self.recent_responses.append(cleaned.lower()[:30])
        if len(self.recent_responses) > 15:
            self.recent_responses.pop(0)



        return cleaned

    def is_response_too_similar(self, response):
        """Check if response is too similar to recent ones"""

        response_lower = response.lower()[:30]

        # Bỏ qua nếu response quá ngắn (dễ false positive)
        if len(response_lower.strip()) < 8:
            return False

        for prev in self.recent_responses[-5:]:
            # Chỉ check exact match — tránh false positive kiểu "i" in "i think..."
            if response_lower == prev:
                return True
            # Check similarity chỉ khi cả 2 đều đủ dài
            if len(prev) >= 15 and len(response_lower) >= 15:
                if response_lower in prev or prev in response_lower:
                    return True

        return False


# ==========================
# CHAT
# ==========================

    def chat(self, user_input):

        self.turn_counter += 1
        intent = self.detect_intent(user_input)
        self.extract_memory(user_input, intent)
        self.update_emotion(user_input)

        # Tóm tắt history nếu đã đủ dài
        self.summarize_history()
        if self.turn_counter % 20 == 0:
            self.consolidate_memory()

        self.last_intent = intent
        
        strategy = self.choose_strategy()
        system_prompt = self.build_natural_prompt(intent, user_input)
        composed_user_input = self.compose_user_message(user_input, intent)

        api_messages = [
            {"role": "system", "content": system_prompt}
        ]

        history = self.messages[-MAX_HISTORY * 2:]
        api_messages.extend(history)

        api_messages.append({
            "role": "user",
            "content": composed_user_input
        })

        # Ý tưởng D: Dynamic Max Tokens
        dynamic_max_tokens = MAX_TOKENS
        if getattr(self, "attention", 5) <= 3:
            dynamic_max_tokens = 40  # Mệt, buồn ngủ -> Rep cộc lốc
        elif getattr(self, "attention", 5) >= 8:
            dynamic_max_tokens = 180 # Hào hứng -> Nói nhiều hơn

        data = {
            "model": self.model,
            "messages": api_messages,
            "temperature": 0.92,
            "max_tokens": dynamic_max_tokens
        }

        reply = "..."
        regenerate_count = 0

        # Thử từng model trong FALLBACK_MODELS
        models_to_try = list(FALLBACK_MODELS)
        if self.model not in models_to_try:
            models_to_try = [self.model] + models_to_try

        for model_name in models_to_try:
            data["model"] = model_name
            print(f"[API] Trying model: {model_name}")
            success = False

            for attempt in range(2):
                try:
                    response = requests.post(
                        BASE_URL,
                        headers=self.headers,
                        json=data,
                        timeout=20,
                        verify=False
                    )

                    result = response.json()
                    print(f"[API] status={response.status_code} model={model_name}")

                    if response.status_code != 200:
                        print(f"[API] {model_name} failed ({response.status_code}), trying next...")
                        break

                    reply = (
                        result.get("choices", [{}])[0]
                        .get("message", {})
                        .get("content", "...")
                        .strip()
                    )

                    if not reply or reply == "...":
                        break

                    if self.is_response_too_similar(reply):
                        regenerate_count += 1
                        if regenerate_count < 3:
                            print(f"Response too similar, regenerating... ({regenerate_count}/3)")
                            continue

                    print(f"[API] Success with: {model_name}")
                    success = True
                    break

                except Exception as e:
                    print(f"[API] ERROR {model_name} (attempt {attempt+1}): {e}")

            if success:
                break

        reply = self.clean_reply(reply)

        # Keep milestone and greeting as internal memory signals only.
        milestone = self.check_relationship_milestone()
        if milestone:
            self.memory["relationship"]["last_milestone_hint"] = milestone

        if self.should_greet:
            self.memory["time_tracking"]["greeting_history"].append({
                "timestamp": datetime.now(VIETNAM_TZ).isoformat(),
                "type": "returning" if self.time_gap_hours is not None and self.time_gap_hours >= 2 else "first",
                "time_period": self.time_period
            })

        self.messages.append({"role": "user", "content": user_input})
        self.messages.append({"role": "assistant", "content": reply})

        # Update conversation stats
        # self.turn_counter has already been incremented at start of chat()
        self.memory["conversation"]["total_messages"] = self.turn_counter
        self.memory["conversation"]["conversation_count"] += 1
        
        # ===== NEW: Update last message time =====
        self.memory["time_tracking"]["last_message_time"] = self.current_time.isoformat()
        self.memory["time_tracking"]["time_gap_hours"] = self.time_gap_hours if self.time_gap_hours else 0
        
        # Save affection + time tracking to persistent memory
        self.save_memory()

        emotion = self.emotion_from_state()

        return {
            "reply": reply,
            "emotion": emotion,
            "mood": round(self.mood, 1),
            "affection": round(self.affection, 1),
            "time_period": self.time_period,
            "time_gap_hours": self.time_gap_hours
        }
