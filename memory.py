# Memory system for Lyra (SQLite-based)

import os
import re
import json
import sqlite3
import threading
from datetime import datetime

try:
    from sentence_transformers import SentenceTransformer
except ImportError:
    SentenceTransformer = None

try:
    import numpy as np
except ImportError:
    np = None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "memory.db")
MEMORY_PATH = os.path.join(BASE_DIR, "memory.json")
MODELS_DIR = os.path.join(BASE_DIR, "models")


class MemorySystem:
    def __init__(self, max_summaries=8):
        self._db_connection = None
        self.db_lock = threading.Lock()
        self._basic_context_cache = None
        self._rag_context_cache = None
        self._rag_cache_key = None
        self._relevant_items_cache = None

        self.max_summaries = max_summaries

        self.memory = self.get_default_memory()
        self.memory_buffer = []
        self.turn_counter = 0
        self._is_dirty = False

        self.encoder = None
        self._embedding_model_name = "paraphrase-multilingual-MiniLM-L12-v2"

    def __getitem__(self, key):
        """Dict-like access for backward compatibility"""
        return self.memory[key]

    def __setitem__(self, key, value):
        """Dict-like access for backward compatibility"""
        self.memory[key] = value

    def __contains__(self, key):
        """Support 'in' operator"""
        return key in self.memory

    def __iter__(self):
        """Support iteration"""
        return iter(self.memory)

    def __len__(self):
        return len(self.memory)

    def _get_db(self):
        """Get DB connection (singleton) with fallback to in-memory mode"""
        # Check existing connection validity
        if self._db_connection is not None:
            try:
                self._db_connection.execute("SELECT 1")
                return self._db_connection
            except sqlite3.Error:
                self._db_connection = None

        try:
            conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=5.0)
            conn.row_factory = sqlite3.Row
            # Enable WAL mode for better concurrency
            conn.execute("PRAGMA journal_mode=WAL")
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
                    embedding BLOB,
                    UNIQUE(kind, value)
                );
            """)
            c.execute("PRAGMA table_info(memory_items)")
            columns = [col[1] for col in c.fetchall()]
            if "embedding" not in columns:
                c.execute("ALTER TABLE memory_items ADD COLUMN embedding BLOB")

            # Check if importance column exists, add if not
            columns_lower = [col.lower() for col in columns]
            if "importance" not in columns_lower:
                c.execute(
                    "ALTER TABLE memory_items ADD COLUMN importance REAL DEFAULT 1.0"
                )

            with self.db_lock:
                conn.commit()
                self._db_connection = conn
            return conn
        except sqlite3.OperationalError as e:
            # Database locked or other operational error - use in-memory fallback
            print(f"[Memory] DB locked or unavailable ({e}), using in-memory mode")
            return self._get_in_memory_fallback()
        except Exception as e:
            print(f"[Memory] DB Connection Error: {e}")
            return self._get_in_memory_fallback()

    def _get_in_memory_fallback(self):
        """Fallback when DB is unavailable - in-memory storage"""

        class InMemoryDB:
            """Mock DB interface for fallback mode"""

            def __init__(self):
                self.data = {
                    "profile": {},
                    "preferences": [],
                    "facts": [],
                    "summaries": [],
                    "conversation": [],
                    "metadata": {},
                    "memory_items": [],
                }
                self._closed = False

            def cursor(self):
                return InMemoryCursor(self)

            def commit(self):
                pass

            def execute(self, sql):
                pass

            def close(self):
                self._closed = True

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        class InMemoryCursor:
            """Mock cursor for fallback mode"""

            def __init__(self, db):
                self.db = db
                self._results = []

            def execute(self, sql, params=None):
                self._results = []
                sql_lower = sql.lower()
                if "select" in sql_lower and "memory_items" in sql_lower:
                    for item in self.db.data.get("memory_items", []):
                        self._results.append(InMemoryRow(item))
                elif "select" in sql_lower and "metadata" in sql_lower:
                    for k, v in self.db.data.get("metadata", {}).items():
                        if params and params[0] == k:
                            self._results.append(InMemoryRow({"key": k, "value": v}))
                return self

            def fetchone(self):
                return self._results[0] if self._results else None

            def fetchall(self):
                return self._results

            def executescript(self, sql):
                pass

            def __iter__(self):
                return iter(self._results)

            @property
            def rowcount(self):
                return len(self._results)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        class InMemoryRow:
            """Mimics sqlite3.Row for in-memory fallback"""

            def __init__(self, data):
                self._data = data
                self._keys = list(data.keys()) if data else []

            def __getitem__(self, key):
                if isinstance(key, int):
                    return list(self._data.values())[key]
                return self._data.get(key)

            def __contains__(self, key):
                return key in self._data

            def keys(self):
                return self._keys

            def get(self, key, default=None):
                return self._data.get(key, default)

        # Return a singleton fallback connection
        if not hasattr(self, "_fallback_conn") or self._fallback_conn is None:
            self._fallback_conn = InMemoryDB()
            print("[Memory] Using in-memory fallback mode")
        return self._fallback_conn

    def get_default_memory(self):
        return {
            "user_profile": {
                "name": None,
                "location": None,
                "age_range": None,
                "occupation": None,
            },
            "preferences": {
                "likes": [],
                "dislikes": [],
                "interests": [],
                "hobbies": [],
            },
            "facts": {
                "personal": [],
                "topics": [],
                "achievements": [],
                "goals": [],
                "inside_jokes": [],
            },
            "conversation": {
                "total_messages": 0,
                "first_chat": None,
                "last_chat": None,
                "conversation_count": 0,
                "favorite_topics": [],
                "chat_history_summary": [],
                "conversation_thread": [],
            },
            "relationship": {
                "current_affection": 50,
                "affection_history": [],
                "trust_level": 0,
                "inside_jokes": [],
                "memorable_moments": [],
                "milestones_reached": [],
            },
            "memory_items": {
                "likes": [],
                "dislikes": [],
                "goals": [],
                "topics": [],
                "episodic": [],
                "relational": [],
            },
            "memory_buffer": [],
            "time_tracking": {
                "last_message_time": None,
                "time_gap_hours": 0,
                "first_greeting_sent": False,
                "greeting_history": [],
            },
            "preferences_ai": {
                "preferred_response_style": "neutral",
                "tone_preference": "casual",
                "length_preference": "short",
            },
            "analytics": {
                "emotion_distribution": {},
                "mood_history": [],
                "daily_stats": {},
                "topic_frequency": {},
            },
        }

    def load(self):
        """Load memory from SQLite"""
        conn = self._get_db()
        if not conn:
            return self.get_default_memory()

        c = conn.cursor()

        if os.path.exists(MEMORY_PATH):
            try:
                with open(MEMORY_PATH, "r", encoding="utf-8") as f:
                    old = json.load(f)
                self._migrate_from_json(c, old)
                with self.db_lock:
                    conn.commit()
                os.rename(MEMORY_PATH, MEMORY_PATH + ".bak")
                print("[Memory] Migrated from memory.json")
            except Exception as e:
                print(f"[Memory] Migration error: {e}")

        db_memory = self._build_memory_dict(c)
        full_memory = self.get_default_memory()

        for key, value in db_memory.items():
            if (
                isinstance(value, dict)
                and key in full_memory
                and isinstance(full_memory[key], dict)
            ):
                full_memory[key].update(value)
            else:
                full_memory[key] = value

        self.memory = full_memory
        self.memory_buffer = self.memory.get("memory_buffer", [])
        self.turn_counter = self.memory.get("conversation", {}).get("total_messages", 0)

        return full_memory

    def _build_memory_dict(self, c):
        def get_meta(key, default=""):
            r = c.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
            return r[0] if r else default

        def get_profile(key):
            r = c.execute("SELECT value FROM profile WHERE key=?", (key,)).fetchone()
            return r[0] if r else None

        memory_rows = list(
            c.execute(
                "SELECT kind, value, weight, saliency, access_count, source_turn, "
                "COALESCE(last_used_at, created_at) AS freshness "
                "FROM memory_items ORDER BY saliency DESC, weight DESC, freshness DESC, id DESC LIMIT 80"
            )
        )

        def from_memory(kind, limit):
            items = [r["value"] for r in memory_rows if r["kind"] == kind]
            return items[:limit] if items else []

        likes = from_memory("like", 20)
        dislikes = from_memory("dislike", 15)
        goals = from_memory("goal", 10)
        topics = from_memory("topic", 10)
        inside_jokes = from_memory("inside_joke", 5)
        episodic = from_memory("episodic", 12)
        relational = from_memory("relational", 8)

        mega = c.execute(
            "SELECT summary, timestamp FROM summaries WHERE is_mega=1 ORDER BY id DESC LIMIT 1"
        ).fetchone()
        recent_summaries = [
            {"summary": r[0], "timestamp": r[1], "is_mega": False}
            for r in c.execute(
                "SELECT summary, timestamp FROM summaries WHERE is_mega=0 ORDER BY id DESC LIMIT ?",
                (self.max_summaries,),
            )
        ]
        recent_summaries.reverse()
        all_summaries = (
            [{"summary": mega[0], "timestamp": mega[1], "is_mega": True}]
            if mega
            else []
        ) + recent_summaries

        messages = [
            {"role": r[0], "content": r[1]}
            for r in c.execute(
                "SELECT role, content FROM conversation ORDER BY id DESC LIMIT 40"
            )
        ]
        messages.reverse()

        return {
            "user_profile": {
                "name": get_profile("name"),
                "location": get_profile("location"),
                "age_range": get_profile("age_range"),
                "occupation": get_profile("occupation"),
            },
            "preferences": {
                "likes": likes,
                "dislikes": dislikes,
                "interests": [],
                "hobbies": [],
            },
            "facts": {
                "personal": [],
                "topics": topics,
                "achievements": [],
                "goals": goals,
                "inside_jokes": inside_jokes,
            },
            "conversation": {
                "total_messages": int(get_meta("total_messages", "0")),
                "first_chat": get_meta("first_chat"),
                "last_chat": get_meta("last_chat"),
                "conversation_count": int(get_meta("conversation_count", "0")),
                "favorite_topics": topics,
                "chat_history_summary": all_summaries,
                "conversation_thread": messages,
            },
            "relationship": {
                "current_affection": int(get_meta("affection", "50")),
                "affection_history": [],
                "trust_level": int(get_meta("trust_level", "0")),
                "inside_jokes": [],
                "memorable_moments": [],
                "milestones_reached": json.loads(get_meta("milestones_reached", "[]")),
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
                "time_gap_hours": 0,
                "first_greeting_sent": False,
                "greeting_history": [],
            },
            "preferences_ai": {
                "preferred_response_style": "neutral",
                "tone_preference": "casual",
                "length_preference": "short",
            },
            "analytics": {
                "emotion_distribution": {},
                "mood_history": [],
                "daily_stats": {},
                "topic_frequency": {},
            },
        }

    def _migrate_from_json(self, c, old):
        for k, v in old.get("user_profile", {}).items():
            if v:
                c.execute("INSERT OR REPLACE INTO profile VALUES (?,?)", (k, str(v)))

        for item in old.get("preferences", {}).get("likes", []):
            c.execute(
                "INSERT OR IGNORE INTO preferences (type,value) VALUES ('like',?)",
                (item,),
            )
            c.execute(
                "INSERT OR IGNORE INTO memory_items (kind,value,weight,saliency,source_turn) VALUES ('like',?,1.0,3,0)",
                (item,),
            )
        for item in old.get("preferences", {}).get("dislikes", []):
            c.execute(
                "INSERT OR IGNORE INTO preferences (type,value) VALUES ('dislike',?)",
                (item,),
            )
            c.execute(
                "INSERT OR IGNORE INTO memory_items (kind,value,weight,saliency,source_turn) VALUES ('dislike',?,1.0,4,0)",
                (item,),
            )
        for item in old.get("facts", {}).get("goals", []):
            c.execute(
                "INSERT OR IGNORE INTO facts (type,value) VALUES ('goal',?)", (item,)
            )
            c.execute(
                "INSERT OR IGNORE INTO memory_items (kind,value,weight,saliency,source_turn) VALUES ('goal',?,1.4,7,0)",
                (item,),
            )
        for item in old.get("conversation", {}).get("favorite_topics", []):
            c.execute(
                "INSERT OR IGNORE INTO facts (type,value) VALUES ('topic',?)", (item,)
            )
            c.execute(
                "INSERT OR IGNORE INTO memory_items (kind,value,weight,saliency,source_turn) VALUES ('topic',?,1.1,4,0)",
                (item,),
            )

        for s in old.get("conversation", {}).get("chat_history_summary", []):
            c.execute(
                "INSERT INTO summaries (summary,timestamp) VALUES (?,?)",
                (s.get("summary", ""), s.get("timestamp", "")),
            )
            if s.get("summary"):
                c.execute(
                    "INSERT OR IGNORE INTO memory_items (kind,value,weight,saliency,source_turn) VALUES ('episodic',?,1.2,5,0)",
                    (s.get("summary", ""),),
                )

        for msg in old.get("conversation", {}).get("conversation_thread", []):
            if isinstance(msg, dict) and msg.get("role") in ("user", "assistant"):
                c.execute(
                    "INSERT INTO conversation (role,content) VALUES (?,?)",
                    (msg["role"], msg["content"]),
                )

        rel = old.get("relationship", {})
        for k, v in [
            ("affection", str(rel.get("current_affection", 50))),
            ("trust_level", str(rel.get("trust_level", 0))),
            ("milestones_reached", json.dumps(rel.get("milestones_reached", []))),
            ("first_chat", old.get("conversation", {}).get("first_chat", "")),
            (
                "total_messages",
                str(old.get("conversation", {}).get("total_messages", 0)),
            ),
            (
                "last_message_time",
                old.get("time_tracking", {}).get("last_message_time", ""),
            ),
        ]:
            if v:
                c.execute("INSERT OR REPLACE INTO metadata VALUES (?,?)", (k, v))

    def save(self):
        """Save memory to SQLite"""
        if not self._is_dirty:
            return

        self._clear_cache()

        conn = self._get_db()
        if not conn:
            return

        c = conn.cursor()
        try:
            from time_utils import get_vietnam_time

            now = get_vietnam_time().isoformat()
        except Exception:
            now = datetime.now().isoformat()

        with self.db_lock:
            for k, v in self.memory.get("user_profile", {}).items():
                if v:
                    c.execute(
                        "INSERT OR REPLACE INTO profile VALUES (?,?)", (k, str(v))
                    )

            for item in self.memory["preferences"].get("likes", []):
                c.execute(
                    "INSERT OR IGNORE INTO preferences (type,value) VALUES ('like',?)",
                    (item,),
                )
            for item in self.memory["preferences"].get("dislikes", []):
                c.execute(
                    "INSERT OR IGNORE INTO preferences (type,value) VALUES ('dislike',?)",
                    (item,),
                )
            for item in self.memory["facts"].get("goals", []):
                c.execute(
                    "INSERT OR IGNORE INTO facts (type,value) VALUES ('goal',?)",
                    (item,),
                )
            for item in self.memory["conversation"].get("favorite_topics", []):
                c.execute(
                    "INSERT OR IGNORE INTO facts (type,value) VALUES ('topic',?)",
                    (item,),
                )

            memory_groups = self.memory.get("memory_items", {})
            weighted_groups = [
                ("like", memory_groups.get("likes", []), 1.0),
                ("dislike", memory_groups.get("dislikes", []), 1.0),
                ("goal", memory_groups.get("goals", []), 1.4),
                ("topic", memory_groups.get("topics", []), 1.1),
                ("episodic", memory_groups.get("episodic", []), 1.2),
                ("relational", memory_groups.get("relational", []), 1.3),
            ]

            # Lazy embedding: compute embeddings on-demand during retrieval, not during save
            # This avoids blocking the main thread during memory save
            pending_embeddings = []  # Track items that need embeddings for background processing

            for kind, values, weight in weighted_groups:
                for value in values[:20]:
                    if value:
                        saliency = self.estimate_saliency(kind, value)
                        existing = c.execute(
                            "SELECT embedding FROM memory_items WHERE kind=? AND value=?",
                            (kind, str(value)),
                        ).fetchone()
                        emb_blob = existing[0] if existing else None

                        # Skip synchronous embedding computation - store without embedding
                        # Embeddings will be computed on-demand during retrieval
                        if emb_blob is None:
                            # Queue for background processing (optional optimization)
                            pending_embeddings.append((kind, str(value)))

                        # Importance-based decay: goals and relational are harder to forget
                        importance = {
                            "goal": 1.5,
                            "relational": 1.4,
                            "like": 1.2,
                            "dislike": 1.2,
                            "episodic": 1.0,
                            "topic": 1.0,
                        }.get(kind, 1.0)

                        c.execute(
                            "INSERT INTO memory_items "
                            "(kind,value,weight,saliency,access_count,source_turn,last_used_at,embedding,importance) "
                            "VALUES (?,?,?,?,0,?,?,?,?) "
                            "ON CONFLICT(kind,value) DO UPDATE SET "
                            "weight=excluded.weight, saliency=excluded.saliency, "
                            "last_used_at=excluded.last_used_at, embedding=excluded.embedding, "
                            "importance=excluded.importance",
                            (
                                kind,
                                str(value),
                                weight,
                                saliency,
                                self.turn_counter,
                                now,
                                emb_blob,
                                importance,
                            ),
                        )

            self.memory["conversation"]["conversation_thread"] = self.memory[
                "conversation"
            ]["conversation_thread"][-40:]
            for msg in self.memory["conversation"]["conversation_thread"][-2:]:
                if isinstance(msg, dict) and msg.get("role"):
                    c.execute(
                        "INSERT INTO conversation (role,content) VALUES (?,?)",
                        (msg["role"], msg.get("content", "")),
                    )

            c.execute("""
                DELETE FROM conversation 
                WHERE id NOT IN (SELECT id FROM conversation ORDER BY id DESC LIMIT 40)
            """)

            for k, v in [
                (
                    "affection",
                    str(self.memory["relationship"].get("current_affection", 50)),
                ),
                ("trust_level", str(self.memory["relationship"].get("trust_level", 0))),
                (
                    "milestones_reached",
                    json.dumps(
                        self.memory["relationship"].get("milestones_reached", [])
                    ),
                ),
                ("last_chat", now),
                ("last_message_time", now),
                ("total_messages", str(self.turn_counter)),
                ("conversation_count", str(self.memory["conversation"].get("conversation_count", 0))),
                ("memory_buffer", json.dumps(self.memory_buffer, ensure_ascii=False)),
            ]:
                c.execute("INSERT OR REPLACE INTO metadata VALUES (?,?)", (k, v))

            if not self.memory["conversation"].get("first_chat"):
                c.execute(
                    "INSERT OR IGNORE INTO metadata VALUES ('first_chat',?)", (now,)
                )

            conn.commit()

        self._is_dirty = False

    def _clear_cache(self):
        self._basic_context_cache = None
        self._rag_context_cache = None
        self._rag_cache_key = None
        self._relevant_items_cache = None

    def estimate_saliency(self, kind, value):
        """Cheap heuristic saliency"""
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
            "stress",
            "stressed",
            "sad",
            "scared",
            "afraid",
            "angry",
            "hurt",
            "love",
            "hate",
            "important",
            "finally",
            "proud",
            "fail",
            "failed",
            "exam",
            "deadline",
            "lonely",
            "miss",
            "anxious",
            "panic",
        ]
        if any(word in text for word in strong_emotion_words):
            score += 3

        if len(text.split()) >= 8:
            score += 1

        return max(1, min(10, score))

    def add_item(self, kind, value, weight=1.0, limit=12):
        """Add memory item with exception handling"""
        self._clear_cache()

        if not value:
            return

        text = str(value).strip()
        if not text:
            return

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

        groups = self.memory.setdefault("memory_items", {})
        items = groups.setdefault(bucket, [])
        if text in items:
            items.remove(text)
        items.insert(0, text)
        groups[bucket] = items[:limit]

        if section and key:
            target_list = self.memory[section].get(key, [])
            if text in target_list:
                target_list.remove(text)
            target_list.insert(0, text)
            self.memory[section][key] = target_list[:bucket_limit]

        # Try to persist to DB, but don't fail if DB is unavailable
        try:
            conn = self._get_db()
            if conn:
                c = conn.cursor()
                saliency = self.estimate_saliency(kind, text)
                now = datetime.now().isoformat()

                # Importance-based decay: goals and relational are harder to forget
                importance = {
                    "goal": 1.5,
                    "relational": 1.4,
                    "like": 1.2,
                    "dislike": 1.2,
                    "episodic": 1.0,
                    "topic": 1.0,
                }.get(kind, 1.0)

                with self.db_lock:
                    existing = c.execute(
                        "SELECT embedding FROM memory_items WHERE kind=? AND value=?",
                        (kind, str(text)),
                    ).fetchone()
                    emb_blob = existing[0] if existing else None

                    c.execute(
                        "INSERT INTO memory_items "
                        "(kind,value,weight,saliency,access_count,source_turn,last_used_at,embedding,importance) "
                        "VALUES (?,?,?,?,0,?,?,?,?) "
                        "ON CONFLICT(kind,value) DO UPDATE SET "
                        "weight=excluded.weight, saliency=excluded.saliency, "
                        "last_used_at=excluded.last_used_at, embedding=excluded.embedding, "
                        "importance=excluded.importance",
                        (
                            kind,
                            str(text),
                            weight,
                            saliency,
                            self.turn_counter,
                            now,
                            emb_blob,
                            importance,
                        ),
                    )
                    conn.commit()
        except Exception as e:
            print(f"[Memory] Failed to persist item to DB: {e}")
            # Fall back to in-memory only

        self._is_dirty = True

    def buffer_candidate(self, kind, value, saliency=None):
        """Stage candidate for memory"""
        text = str(value or "").strip()
        if not text:
            return

        if saliency is None:
            saliency = self.estimate_saliency(kind, text)

        for item in self.memory_buffer:
            if (
                item.get("kind") == kind
                and item.get("value", "").lower() == text.lower()
            ):
                item["saliency"] = max(item.get("saliency", 0), saliency)
                item["count"] = item.get("count", 1) + 1
                item["last_seen_turn"] = self.turn_counter
                self.memory["memory_buffer"] = self.memory_buffer
                return

        self.memory_buffer.append(
            {
                "kind": kind,
                "value": text[:160],
                "saliency": saliency,
                "count": 1,
                "last_seen_turn": self.turn_counter,
            }
        )

        if len(self.memory_buffer) > 24:
            self.memory_buffer = self.memory_buffer[-24:]

        self.memory["memory_buffer"] = self.memory_buffer

    def should_buffer(self, text, intent=None):
        """Cheap gate before AI extraction"""
        cleaned = (text or "").strip()
        if len(cleaned) < 8:
            return False

        lowered = cleaned.lower()
        memory_keywords = [
            "i like",
            "i love",
            "i hate",
            "my goal",
            "i want to",
            "i need to",
            "i'm trying to",
            "i am trying to",
            "i feel",
            "i felt",
            "i was",
            "remember",
            "i have",
            "my exam",
            "deadline",
            "project",
            "school",
            "work",
            "stress",
            "stressed",
            "anxious",
            "sad",
            "proud",
            "finally",
        ]
        if any(keyword in lowered for keyword in memory_keywords):
            return True

        if intent in ("introduction", "complaint", "request"):
            return True

        return len(cleaned.split()) >= 14

    def extract_candidates_heuristic(self, text):
        """Collect low-cost candidates"""
        cleaned = (text or "").strip()
        lowered = cleaned.lower()
        candidates = []

        def add(kind, value):
            value = (value or "").strip(" .,!?\n\t")
            if value:
                candidates.append(
                    {
                        "kind": kind,
                        "value": value[:160],
                        "saliency": self.estimate_saliency(kind, value),
                    }
                )

        like_patterns = [r"(?:i like|i love|i'm into|i am into)\s+(.+)"]
        dislike_patterns = [r"(?:i hate|i dislike|i can't stand)\s+(.+)"]
        goal_patterns = [
            r"(?:i want to|i need to|i'm trying to|i am trying to|my goal is to)\s+(.+)"
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

        topic_keywords = [
            "math",
            "code",
            "coding",
            "python",
            "exam",
            "study",
            "school",
            "work",
            "project",
        ]
        for keyword in topic_keywords:
            if keyword in lowered:
                add("topic", keyword)

        if any(
            word in lowered
            for word in [
                "stressed",
                "sad",
                "anxious",
                "deadline",
                "exam",
                "proud",
                "finally",
            ]
        ):
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

    def should_flush(self, intent=None):
        """Flush buffer after enough signal"""
        if not self.memory_buffer:
            return False
        if len(self.memory_buffer) >= 6:
            return True
        if any(item.get("saliency", 0) >= 7 for item in self.memory_buffer):
            return True
        return self.turn_counter % 6 == 0 and len(self.memory_buffer) >= 3

    def flush_buffer(self, recent_messages, user_input=""):
        """Flush buffer to memory via AI"""
        if not self.memory_buffer:
            return []

        candidates = [
            {
                "kind": item.get("kind"),
                "value": item.get("value"),
                "saliency": item.get("saliency", 0),
                "count": item.get("count", 1),
            }
            for item in self.memory_buffer[-8:]
        ]

        convo_snippet = ""
        for msg in recent_messages[-6:]:
            if isinstance(msg, dict) and "role" in msg and "content" in msg:
                role = "User" if msg["role"] == "user" else "Lyra"
                convo_snippet += f"{role}: {msg['content']}\n"
        if user_input:
            convo_snippet += f"User: {user_input}\n"

        try:
            from config import DEFAULT_MODEL, BASE_URL
            import requests

            from config import API_KEY

            response = requests.post(
                BASE_URL,
                headers={
                    "Authorization": f"Bearer {API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": DEFAULT_MODEL,
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
                            ),
                        },
                        {
                            "role": "user",
                            "content": f"Recent chat:\n{convo_snippet}\nCandidates:\n{json.dumps(candidates, ensure_ascii=False)}",
                        },
                    ],
                    "temperature": 0.1,
                    "max_tokens": 220,
                },
                timeout=15,
                verify=False,
            )
            raw = (
                response.json()
                .get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
                .strip()
            )
            raw = re.sub(r"```json|```", "", raw).strip()
            result = json.loads(raw) if raw else {}
            kept = result.get("memories", [])
        except Exception as e:
            print(f"[Memory] flush failed: {e}")
            kept = [item for item in candidates if item.get("saliency", 0) >= 6][:3]

        for item in kept:
            kind = item.get("kind")
            value = item.get("value")
            if kind and value:
                self.add_item(kind, value, weight=1.0, limit=12)

        self.memory_buffer.clear()
        self.memory["memory_buffer"] = self.memory_buffer

        return kept

    def get_context(self, user_input=""):
        """Get memory context for prompt"""
        if self._basic_context_cache is not None:
            return self._basic_context_cache

        try:
            profile = self.memory.get("user_profile", {})
            prefs = self.memory.get("preferences", {})
            facts = self.memory.get("facts", {})

            parts = []

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

            if prefs.get("likes"):
                parts.append("Likes: " + ", ".join(prefs["likes"][:6]))
            if prefs.get("dislikes"):
                parts.append("Dislikes: " + ", ".join(prefs["dislikes"][:4]))

            topics = self.memory.get("conversation", {}).get("favorite_topics", [])
            if topics:
                parts.append("Into: " + ", ".join(topics[:6]))
            if facts.get("goals"):
                parts.append("Goals: " + ", ".join(facts["goals"][:3]))

            if not parts:
                self._basic_context_cache = ""
                return ""

            self._basic_context_cache = "What you know about them:\n" + "\n".join(
                f"- {p}" for p in parts
            )
            return self._basic_context_cache

        except Exception as e:
            print(f"[Memory] Error building context: {e}")
            return ""

    def get_focused_context(self, user_input=""):
        """Get only 2-3 most relevant memory items - simplified for natural conversation"""
        try:
            profile = self.memory.get("user_profile", {})
            episodic = self.memory.get("facts", {}).get("episodic", [])[:2]

            parts = []

            if profile.get("name"):
                parts.append(f"Name: {profile['name']}")

            if episodic:
                parts.append(f"Recent: {episodic[0][:60]}")

            return "\n".join(parts) if parts else ""
        except Exception as e:
            print(f"[Memory] get_focused_context error: {e}")
            return ""

    def get_relevant_context(self, user_input):
        """Semantic search for relevant memory"""
        # Include turn_counter in cache key so same input at different turns returns different results
        cache_key = f"rag_{self.turn_counter}_{user_input.strip().lower()}"
        if self._rag_context_cache and cache_key == self._rag_cache_key:
            return self._rag_context_cache

        query_vector = self._get_embedding(user_input)

        try:
            if self._relevant_items_cache is None:
                conn = self._get_db()
                if conn is None:
                    return ""
                c = conn.cursor()
                self._relevant_items_cache = list(
                    c.execute(
                        "SELECT kind, value, saliency, embedding FROM memory_items "
                        "WHERE kind NOT IN ('episodic') "
                        "ORDER BY saliency DESC, created_at DESC LIMIT 100"
                    )
                )

            rows = self._relevant_items_cache

            if query_vector is not None and np is not None:
                scored = []
                for r in rows:
                    if r["embedding"]:
                        try:
                            vector = np.frombuffer(r["embedding"], dtype=np.float32)
                            score = self._cosine_similarity(query_vector, vector)
                            final_score = (score * 0.8) + (
                                min(1, r["saliency"] / 10) * 0.2
                            )
                            scored.append((final_score, r))
                        except Exception:
                            continue

                if scored:
                    scored.sort(key=lambda x: x[0], reverse=True)
                    top_memories = [x[1]["value"] for x in scored[:6] if x[0] > 0.45]

                    if top_memories:
                        result = "Relevant memory highlights:\n" + "\n".join(
                            f"- {m}" for m in top_memories
                        )
                        self._rag_cache_key = cache_key
                        self._rag_context_cache = result
                        return result

            # Fallback to keyword search
            query_tokens = self._tokenize(user_input)
            if not query_tokens:
                return ""

            candidates = []
            profile = self.memory.get("user_profile", {})
            if profile.get("name"):
                candidates.append(f"Their name is {profile['name']}")
            if profile.get("location"):
                candidates.append(f"They are from {profile['location']}")

            if not rows:
                conn = self._get_db()
                if conn:
                    c = conn.cursor()
                    rows = list(
                        c.execute(
                            "SELECT kind, value, weight, saliency, access_count FROM memory_items "
                            "WHERE kind NOT IN ('episodic') "
                            "ORDER BY saliency DESC, access_count DESC LIMIT 40"
                        )
                    )

            for row in rows:
                kind = row["kind"]
                raw_value = row["value"]
                if kind == "like":
                    candidates.append(f"They like {raw_value}")
                elif kind == "dislike":
                    candidates.append(f"They dislike {raw_value}")
                elif kind == "goal":
                    candidates.append(f"They mentioned a goal: {raw_value}")
                elif kind == "topic":
                    candidates.append(f"They often bring up {raw_value}")

            scored = []
            for text in candidates:
                tokens = self._tokenize(text)
                overlap = len(query_tokens & tokens)
                if overlap:
                    scored.append((overlap, text))

            if not scored:
                return ""

            scored.sort(key=lambda x: x[0], reverse=True)
            selected = [t[1] for t in scored[:3]]

            result = "Relevant memory:\n" + "\n".join(f"- {t}" for t in selected)
            self._rag_cache_key = cache_key
            self._rag_context_cache = result
            return result

        except Exception as e:
            print(f"[Memory] RAG error: {e}")
            return ""

    def _tokenize(self, text):
        if not text:
            return set()
        return {
            token
            for token in re.findall(r"[a-zA-Z0-9']+", text.lower())
            if len(token) >= 3
        }

    def _cosine_similarity(self, v1, v2):
        if v1 is None or v2 is None or np is None:
            return 0
        dot = np.dot(v1, v2)
        norm1 = np.linalg.norm(v1)
        norm2 = np.linalg.norm(v2)
        if norm1 == 0 or norm2 == 0:
            return 0
        return dot / (norm1 * norm2)

    def _get_embedding(self, text):
        if SentenceTransformer is None or np is None:
            return None

        try:
            local_path = os.path.join(MODELS_DIR, self._embedding_model_name)

            if self.encoder is None:
                if os.path.exists(local_path):
                    self.encoder = SentenceTransformer(local_path)
                else:
                    self.encoder = SentenceTransformer(self._embedding_model_name)
                    if not os.path.exists(MODELS_DIR):
                        os.makedirs(MODELS_DIR)
                    self.encoder.save(local_path)

            return self.encoder.encode(text)
        except Exception:
            return None

    def touch_items(self, items):
        """Refresh recency for retrieved items"""
        if not items:
            return

        conn = self._get_db()
        if not conn:
            return

        c = conn.cursor()
        now = datetime.now().isoformat()

        with self.db_lock:
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
                        "UPDATE memory_items SET last_used_at=?, access_count=MIN(access_count+1, 10000) "
                        "WHERE kind=? AND value=?",
                        (now, db_kind, value),
                    )
            conn.commit()

    def consolidate(self):
        """Compress and forget stale memories"""
        try:
            conn = self._get_db()
            if not conn:
                return

            c = conn.cursor()

            with self.db_lock:
                # Delete items that:
                # - Have never been accessed (access_count = 0)
                # - Are older than 100 turns
                # - Have low saliency AND low importance (both must be low to delete)
                # This prevents important but rarely-accessed memories from being deleted
                # Use COALESCE for backwards compatibility with old items without importance column
                c.execute(
                    """
                    DELETE FROM memory_items 
                    WHERE access_count = 0 
                    AND (? - source_turn) > 100 
                    AND saliency < 7 
                    AND COALESCE(importance, 1.0) < 1.3
                """,
                    (self.turn_counter,),
                )

                deleted_count = c.rowcount
                if deleted_count > 0:
                    print(f"[Memory] Forgot {deleted_count} stale items.")

                conn.commit()
        except Exception as e:
            # Silently ignore if importance column doesn't exist yet
            if "no such column" not in str(e).lower():
                print(f"[Memory] Consolidate error: {e}")
