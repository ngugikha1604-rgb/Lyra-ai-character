# Memory system for Lyra (SQLite + Pinecone hybrid)
# Architecture:
#   L1 — User Memory   : facts bất biến về user (profile, likes, goals) → SQLite only, luôn inject
#   L2 — Session Memory: context stream/chat hôm nay → SQLite only, xóa sau session
#   L3 — Temporal      : episodic + summaries theo thời gian → SQLite + Pinecone vector search

import os
import re
import json
import sqlite3
import threading
import requests
import random
from datetime import datetime, timedelta

try:
    import numpy as np
except ImportError:
    np = None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH  = os.path.join(BASE_DIR, "memory.db")
MEMORY_PATH = os.path.join(BASE_DIR, "memory.json")
MODELS_DIR  = os.path.join(BASE_DIR, "models")
# Global lock cho SQLite để tránh "database is locked" giữa các thread/class
DB_LOCK = threading.Lock()

# Layer constants
LAYER_USER    = "user"     # L1 — bất biến, luôn inject
LAYER_SESSION = "session"  # L2 — chỉ trong stream/chat hôm nay
LAYER_TEMPORAL = "temporal" # L3 — episodic, summaries, sự kiện theo thời gian

# Kinds thuộc từng layer
_LAYER_MAP = {
    "like":      LAYER_USER,
    "dislike":   LAYER_USER,
    "goal":      LAYER_USER,
    "topic":     LAYER_USER,
    "relational": LAYER_USER,
    "inside_joke": LAYER_USER,
    "episodic":  LAYER_TEMPORAL,
    "session":   LAYER_SESSION,
}

# Conflict detection: các kind có thể mâu thuẫn với nhau
_CONFLICTABLE_KINDS = {"like", "dislike", "goal", "relational"}


# ══════════════════════════════════════════════════════════════════════════════
# Embedding via Ollama (thay SentenceTransformer — không tốn RAM)
# ══════════════════════════════════════════════════════════════════════════════

def _get_ollama_embedding(text: str) -> "np.ndarray | None":
    """
    Gọi Ollama /api/embeddings để lấy vector.
    Model: nomic-embed-text (384 dims, nhẹ, nhanh).
    Fallback về None nếu Ollama không available.
    """
    if np is None:
        return None
    try:
        from config import EMBEDDING_MODEL, EMBEDDING_URL
        resp = requests.post(
            EMBEDDING_URL,
            json={"model": EMBEDDING_MODEL, "prompt": text},
            timeout=5,
            verify=False,
        )
        if resp.status_code == 200:
            vec = resp.json().get("embedding")
            if vec:
                return np.array(vec, dtype=np.float32)
    except Exception:
        pass
    return None


def _cosine_similarity(v1, v2) -> float:
    if v1 is None or v2 is None or np is None:
        return 0.0
    dot   = np.dot(v1, v2)
    norm1 = np.linalg.norm(v1)
    norm2 = np.linalg.norm(v2)
    if norm1 == 0 or norm2 == 0:
        return 0.0
    return float(dot / (norm1 * norm2))


# ══════════════════════════════════════════════════════════════════════════════
# Pinecone Layer — chỉ dùng cho L3 Temporal
# ══════════════════════════════════════════════════════════════════════════════

class PineconeLayer:
    """
    Thin wrapper quanh Pinecone REST API (không dùng SDK nặng).
    Chỉ xử lý L3 Temporal (episodic + summaries).
    Dùng requests trực tiếp → không cần install pinecone-client.
    """

    def __init__(self):
        from config import PINECONE_API_KEY, PINECONE_INDEX
        self.api_key   = PINECONE_API_KEY
        self.index_name = PINECONE_INDEX
        self._host: str | None = None  # lazy-loaded
        self._enabled = bool(self.api_key)
        self.dimension = None # Auto-detected
        if not self._enabled:
            print("[Pinecone] No API key — L3 vector search disabled.")
        else:
            self._detect_dimension()

    def _detect_dimension(self):
        """Tự động detect dimension bằng cách probe embedding model."""
        try:
            sample_vec = _get_ollama_embedding("probe")
            if sample_vec is not None:
                self.dimension = len(sample_vec)
                print(f"[Pinecone] Auto-detected dimension: {self.dimension}")
        except Exception as e:
            print(f"[Pinecone] Dimension detection error: {e}")

    def _get_host(self) -> str | None:
        """Lấy host URL của index (lazy, cache lại)."""
        if self._host:
            return self._host
        if not self._enabled:
            return None
        try:
            resp = requests.get(
                f"https://api.pinecone.io/indexes/{self.index_name}",
                headers={"Api-Key": self.api_key},
                timeout=10,
            )
            if resp.status_code == 200:
                self._host = resp.json().get("host")
                return self._host
            elif resp.status_code == 404:
                # Index chưa tồn tại → tạo mới
                self._create_index()
                return self._get_host()
        except Exception as e:
            print(f"[Pinecone] get_host error: {e}")
        return None

    def _create_index(self):
        """Tạo serverless index trên free tier (us-east-1, aws)."""
        try:
            resp = requests.post(
                "https://api.pinecone.io/indexes",
                headers={"Api-Key": self.api_key, "Content-Type": "application/json"},
                json={
                    "name": self.index_name,
                    "dimension": self.dimension or 768,   # Dùng dimension auto-detected
                    "metric": "cosine",
                    "spec": {
                        "serverless": {"cloud": "aws", "region": "us-east-1"}
                    },
                },
                timeout=30,
            )
            if resp.status_code in (200, 201):
                print(f"[Pinecone] Index '{self.index_name}' created.")
            else:
                print(f"[Pinecone] Create index failed: {resp.status_code} {resp.text}")
        except Exception as e:
            print(f"[Pinecone] create_index error: {e}")

    def upsert(self, item_id: str, vector: list, metadata: dict):
        """Upsert 1 vector vào Pinecone."""
        host = self._get_host()
        if not host or not self._enabled:
            return
        try:
            requests.post(
                f"https://{host}/vectors/upsert",
                headers={"Api-Key": self.api_key, "Content-Type": "application/json"},
                json={"vectors": [{"id": item_id, "values": vector, "metadata": metadata}]},
                timeout=10,
            )
        except Exception as e:
            print(f"[Pinecone] upsert error: {e}")

    def query(self, vector: list, top_k: int = 6, filter_meta: dict = None) -> list:
        """
        Semantic search trong Pinecone.
        Trả về list dict: [{id, score, metadata}]
        """
        host = self._get_host()
        if not host or not self._enabled:
            return []
        try:
            body = {
                "vector": vector,
                "topK": top_k,
                "includeMetadata": True,
            }
            if filter_meta:
                body["filter"] = filter_meta
            resp = requests.post(
                f"https://{host}/query",
                headers={"Api-Key": self.api_key, "Content-Type": "application/json"},
                json=body,
                timeout=10,
            )
            if resp.status_code == 200:
                matches = resp.json().get("matches", [])
                return [
                    {
                        "id":       m["id"],
                        "score":    m["score"],
                        "metadata": m.get("metadata", {}),
                    }
                    for m in matches
                ]
        except Exception as e:
            print(f"[Pinecone] query error: {e}")
        return []

    def delete(self, item_id: str):
        """Xóa 1 vector khỏi Pinecone."""
        host = self._get_host()
        if not host or not self._enabled:
            return
        try:
            requests.post(
                f"https://{host}/vectors/delete",
                headers={"Api-Key": self.api_key, "Content-Type": "application/json"},
                json={"ids": [item_id]},
                timeout=10,
            )
        except Exception as e:
            print(f"[Pinecone] delete error: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# MemoryRanker — Using qwen2.5:0.5b to prioritize context
# ══════════════════════════════════════════════════════════════════════════════

class MemoryRanker:
    """
    Ranks memory items based on their relevance to the current user input.
    Uses qwen2.5:0.5b (LIGHT_MODEL) for high-speed, numeric scoring.
    """
    def __init__(self):
        try:
            from config import LIGHT_MODEL, LIGHT_BASE_URL
            self.model = LIGHT_MODEL or "qwen2.5:0.5b"
            self.url = LIGHT_BASE_URL or "http://localhost:11434/api/chat"
        except ImportError:
            self.model = "qwen2.5:0.5b"
            self.url = "http://localhost:11434/api/chat"

    def _call_scoring_model(self, query: str, candidates: list[str]) -> list[float]:
        """Calls light model to get relevance scores (1-10) for candidates."""
        if not candidates:
            return []
        
        # Batch scoring to save time/tokens
        prompt = (
            f"Query: \"{query}\"\n"
            "Rank how relevant each item is to the query (1-10, 10 is most relevant).\n"
            "Return ONLY a comma-separated list of numbers.\n"
            "Items:\n"
        )
        for i, cand in enumerate(candidates):
            prompt += f"{i+1}. {cand[:120]}\n"

        try:
            resp = requests.post(
                self.url,
                json={
                    "model": self.model,
                    "messages": [{"role": "user", "content": prompt}],
                    "options": {"temperature": 0.1, "num_predict": 30},
                    "stream": False
                },
                timeout=8,
                verify=False
            )
            if resp.status_code == 200:
                content = resp.json().get("message", {}).get("content", "").strip()
                # Extract numbers
                scores = [float(s.strip()) for s in re.findall(r"\b\d+\b", content)]
                # Ensure length matches
                if len(scores) < len(candidates):
                    scores.extend([1.0] * (len(candidates) - len(scores)))
                return scores[:len(candidates)]
        except Exception:
            pass
        return [1.0] * len(candidates)

    def rank(self, query: str, items: list[dict], token_budget: int = 550) -> list[str]:
        """
        Sorts items by Score * Weight and fits them into the token budget.
        Items list: [{"kind": kind, "value": value, "weight": weight}]
        """
        if not items:
            return []

        candidates_text = [i["value"] for i in items]
        scores = self._call_scoring_model(query, candidates_text)

        scored_items = []
        for i, item in enumerate(items):
            # Final Score = AI Relevancy * Saliency/Kind Weight
            relevancy = scores[i] if i < len(scores) else 1.0
            final_score = relevancy * item.get("weight", 1.0)
            scored_items.append((final_score, item["value"]))

        # Sort descending
        scored_items.sort(key=lambda x: x[0], reverse=True)

        # Fit to budget (roughly 4 chars per token)
        result = []
        current_chars = 0
        char_limit = token_budget * 3.8 # Heuristic buffer

        for _, text in scored_items:
            text_len = len(text)
            if current_chars + text_len > char_limit:
                break
            result.append(text)
            current_chars += text_len

        return result


# ══════════════════════════════════════════════════════════════════════════════
# MemorySystem
# ══════════════════════════════════════════════════════════════════════════════

class MemorySystem:
    def __init__(self, max_summaries=8):
        self._db_connection = None
        self.db_lock = DB_LOCK # Dùng chung khóa toàn cục
        self._basic_context_cache  = None
        self._rag_context_cache    = None
        self._rag_cache_key        = None
        self._relevant_items_cache = None

        self.max_summaries  = max_summaries
        self.memory         = self.get_default_memory()
        self.memory_buffer  = []
        self.turn_counter   = 0
        self._is_dirty      = False

        # Pinecone — L3 only
        self.pinecone = PineconeLayer()
        self.ranker   = MemoryRanker()

        # Session memory (L2) — in-memory, cleared on stream stop
        self._session_items: list[dict] = []
        self._session_last_activity_at: str | None = None
        self._rolling_stream_summary: str = ""

    # ── dict-like access (backward compat) ────────────────────────────────────
    def __getitem__(self, key):  return self.memory[key]
    def __setitem__(self, key, value): self.memory[key] = value
    def __contains__(self, key): return key in self.memory
    def __iter__(self):          return iter(self.memory)
    def __len__(self):           return len(self.memory)

    # ── DB ─────────────────────────────────────────────────────────────────────
    def _get_db(self):
        if self._db_connection is not None:
            try:
                self._db_connection.execute("SELECT 1")
                return self._db_connection
            except sqlite3.Error:
                self._db_connection = None
        try:
            conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=5.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            c = conn.cursor()
            c.executescript("""
                CREATE TABLE IF NOT EXISTS profile (key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE IF NOT EXISTS self_profile (key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE IF NOT EXISTS preferences (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    type TEXT NOT NULL, value TEXT NOT NULL, UNIQUE(type, value)
                );
                CREATE TABLE IF NOT EXISTS facts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    type TEXT NOT NULL, value TEXT NOT NULL, UNIQUE(type, value)
                );
                CREATE TABLE IF NOT EXISTS summaries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    summary TEXT NOT NULL, timestamp TEXT NOT NULL,
                    is_mega INTEGER DEFAULT 0,
                    created_at TEXT DEFAULT (datetime('now'))
                );
                CREATE TABLE IF NOT EXISTS conversation (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    role TEXT NOT NULL, content TEXT NOT NULL,
                    created_at TEXT DEFAULT (datetime('now'))
                );
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE IF NOT EXISTS memory_items (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind        TEXT NOT NULL,
                    value       TEXT NOT NULL,
                    layer       TEXT NOT NULL DEFAULT 'user',
                    weight      REAL DEFAULT 1.0,
                    saliency    REAL DEFAULT 0,
                    importance  REAL DEFAULT 1.0,
                    access_count INTEGER DEFAULT 0,
                    source_turn  INTEGER DEFAULT 0,
                    last_used_at TEXT,
                    created_at   TEXT DEFAULT (datetime('now')),
                    embedding    BLOB,
                    pinecone_id  TEXT,
                    superseded   INTEGER DEFAULT 0,
                    UNIQUE(kind, value)
                );
                CREATE TABLE IF NOT EXISTS stream_milestones (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type  TEXT NOT NULL,
                    description TEXT NOT NULL,
                    achieved_at TEXT NOT NULL,
                    stream_title TEXT DEFAULT '',
                    peak_viewers INTEGER DEFAULT 0,
                    UNIQUE(event_type)
                );
                CREATE TABLE IF NOT EXISTS memory_conflicts (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind        TEXT NOT NULL,
                    old_value   TEXT NOT NULL,
                    new_value   TEXT NOT NULL,
                    resolved_at TEXT NOT NULL,
                    note        TEXT DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS diaries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    content TEXT NOT NULL,
                    mood_score REAL,
                    affection_score REAL,
                    timestamp TEXT DEFAULT (datetime('now'))
                );
            """)
            # Migration: thêm columns mới nếu DB cũ chưa có
            existing_cols = {row[1] for row in c.execute("PRAGMA table_info(memory_items)")}
            for col, definition in [
                ("layer",       "TEXT NOT NULL DEFAULT 'user'"),
                ("importance",  "REAL DEFAULT 1.0"),
                ("pinecone_id", "TEXT"),
                ("superseded",  "INTEGER DEFAULT 0"),
            ]:
                if col not in existing_cols:
                    c.execute(f"ALTER TABLE memory_items ADD COLUMN {col} {definition}")

            with self.db_lock:
                conn.commit()
                self._db_connection = conn
            return conn
        except Exception as e:
            print(f"[Memory] DB error: {e}")
            return self._get_in_memory_fallback()

    # ── Default memory structure ───────────────────────────────────────────────
    def get_default_memory(self):
        return {
            "user_profile": {"name": None, "location": None, "age_range": None, "occupation": None},
            "preferences":  {"likes": [], "dislikes": [], "interests": [], "hobbies": []},
            "facts":        {"personal": [], "topics": [], "achievements": [], "goals": [], "inside_jokes": []},
            "conversation": {
                "total_messages": 0, "first_chat": None, "last_chat": None,
                "conversation_count": 0, "favorite_topics": [],
                "chat_history_summary": [], "conversation_thread": [],
            },
            "relationship": {
                "current_affection": 50, "affection_history": [], "trust_level": 0,
                "inside_jokes": [], "memorable_moments": [], "milestones_reached": [],
            },
            "memory_items": {"likes": [], "dislikes": [], "goals": [], "topics": [], "episodic": [], "relational": []},
            "memory_buffer": [],
            "time_tracking": {"last_message_time": None, "time_gap_hours": 0, "first_greeting_sent": False, "greeting_history": []},
            "preferences_ai": {"preferred_response_style": "neutral", "tone_preference": "casual", "length_preference": "short"},
            "analytics": {"emotion_distribution": {}, "mood_history": [], "daily_stats": {}, "topic_frequency": {}},
            "identity": {"name": "Lyra", "gender": "Nữ", "occupation": "VTuber"},
        }


    # ── Load ───────────────────────────────────────────────────────────────────
    def load(self):
        conn = self._get_db()
        if not conn:
            return self.get_default_memory()

        c = conn.cursor()

        # Migrate từ JSON cũ nếu còn
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
        full = self.get_default_memory()
        for key, value in db_memory.items():
            if isinstance(value, dict) and key in full and isinstance(full[key], dict):
                full[key].update(value)
            else:
                full[key] = value

        self.memory       = full
        self.memory_buffer = self.memory.get("memory_buffer", [])
        self.turn_counter  = self.memory.get("conversation", {}).get("total_messages", 0)
        return full

    def _build_memory_dict(self, c):
        def get_meta(key, default=""):
            r = c.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
            return r[0] if r else default

        def get_profile(key):
            r = c.execute("SELECT value FROM profile WHERE key=?", (key,)).fetchone()
            return r[0] if r else None

        def get_self_profile(key):
            r = c.execute("SELECT value FROM self_profile WHERE key=?", (key,)).fetchone()
            return r[0] if r else None

        # Chỉ load L1 User Memory vào RAM — L3 Temporal được query on-demand
        memory_rows = list(c.execute(
            "SELECT kind, value, weight, saliency, access_count, source_turn, "
            "COALESCE(last_used_at, created_at) AS freshness "
            "FROM memory_items "
            "WHERE layer=? AND superseded=0 "
            "ORDER BY saliency DESC, weight DESC, freshness DESC, id DESC LIMIT 80",
            (LAYER_USER,)
        ))

        def from_memory(kind, limit):
            return [r["value"] for r in memory_rows if r["kind"] == kind][:limit]

        likes        = from_memory("like", 20)
        dislikes     = from_memory("dislike", 15)
        goals        = from_memory("goal", 10)
        topics       = from_memory("topic", 10)
        inside_jokes = from_memory("inside_joke", 5)
        relational   = from_memory("relational", 8)

        # L3 Temporal: episodic từ SQLite (Pinecone dùng khi semantic search)
        episodic_rows = list(c.execute(
            "SELECT value FROM memory_items WHERE kind='episodic' AND superseded=0 "
            "ORDER BY created_at DESC LIMIT 12"
        ))
        episodic = [r["value"] for r in episodic_rows]

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
        all_summaries = ([{"summary": mega[0], "timestamp": mega[1], "is_mega": True}] if mega else []) + recent_summaries

        messages = [
            {"role": r[0], "content": r[1]}
            for r in c.execute("SELECT role, content FROM conversation ORDER BY id DESC LIMIT 40")
        ]
        messages.reverse()

        return {
            "user_profile": {
                "name": get_profile("name"), "location": get_profile("location"),
                "age_range": get_profile("age_range"), "occupation": get_profile("occupation"),
            },
            "identity": {
                "name": get_self_profile("name") or "Lyra",
                "gender": get_self_profile("gender") or "Nữ",
                "occupation": get_self_profile("occupation") or "VTuber",
            },
            "preferences": {"likes": likes, "dislikes": dislikes, "interests": [], "hobbies": []},
            "facts": {"personal": [], "topics": topics, "achievements": [], "goals": goals, "inside_jokes": inside_jokes},
            "conversation": {
                "total_messages": int(get_meta("total_messages", "0")),
                "first_chat": get_meta("first_chat"), "last_chat": get_meta("last_chat"),
                "conversation_count": int(get_meta("conversation_count", "0")),
                "favorite_topics": topics, "chat_history_summary": all_summaries,
                "conversation_thread": messages,
            },
            "relationship": {
                "current_affection": int(get_meta("affection", "50")),
                "affection_history": [], "trust_level": int(get_meta("trust_level", "0")),
                "inside_jokes": [], "memorable_moments": [],
                "milestones_reached": json.loads(get_meta("milestones_reached", "[]")),
            },
            "memory_items": {
                "likes": likes, "dislikes": dislikes, "goals": goals,
                "topics": topics, "episodic": episodic, "relational": relational,
            },
            "memory_buffer": json.loads(get_meta("memory_buffer", "[]")),
            "time_tracking": {
                "last_message_time": get_meta("last_message_time"),
                "time_gap_hours": 0, "first_greeting_sent": False, "greeting_history": [],
            },
            "preferences_ai": {"preferred_response_style": "neutral", "tone_preference": "casual", "length_preference": "short"},
            "analytics": {"emotion_distribution": {}, "mood_history": [], "daily_stats": {}, "topic_frequency": {}},
        }

    def _migrate_from_json(self, c, old):
        for k, v in old.get("user_profile", {}).items():
            if v:
                c.execute("INSERT OR REPLACE INTO profile VALUES (?,?)", (k, str(v)))
        for item in old.get("preferences", {}).get("likes", []):
            c.execute("INSERT OR IGNORE INTO preferences (type,value) VALUES ('like',?)", (item,))
            c.execute("INSERT OR IGNORE INTO memory_items (kind,value,layer,weight,saliency,source_turn) VALUES ('like',?,?,1.0,3,0)", (item, LAYER_USER))
        for item in old.get("preferences", {}).get("dislikes", []):
            c.execute("INSERT OR IGNORE INTO preferences (type,value) VALUES ('dislike',?)", (item,))
            c.execute("INSERT OR IGNORE INTO memory_items (kind,value,layer,weight,saliency,source_turn) VALUES ('dislike',?,?,1.0,4,0)", (item, LAYER_USER))
        for item in old.get("facts", {}).get("goals", []):
            c.execute("INSERT OR IGNORE INTO facts (type,value) VALUES ('goal',?)", (item,))
            c.execute("INSERT OR IGNORE INTO memory_items (kind,value,layer,weight,saliency,source_turn) VALUES ('goal',?,?,1.4,7,0)", (item, LAYER_USER))
        for item in old.get("conversation", {}).get("favorite_topics", []):
            c.execute("INSERT OR IGNORE INTO facts (type,value) VALUES ('topic',?)", (item,))
            c.execute("INSERT OR IGNORE INTO memory_items (kind,value,layer,weight,saliency,source_turn) VALUES ('topic',?,?,1.1,4,0)", (item, LAYER_USER))
        for s in old.get("conversation", {}).get("chat_history_summary", []):
            c.execute("INSERT INTO summaries (summary,timestamp) VALUES (?,?)", (s.get("summary", ""), s.get("timestamp", "")))
            if s.get("summary"):
                c.execute("INSERT OR IGNORE INTO memory_items (kind,value,layer,weight,saliency,source_turn) VALUES ('episodic',?,?,1.2,5,0)", (s.get("summary", ""), LAYER_TEMPORAL))
        for msg in old.get("conversation", {}).get("conversation_thread", []):
            if isinstance(msg, dict) and msg.get("role") in ("user", "assistant"):
                c.execute("INSERT INTO conversation (role,content) VALUES (?,?)", (msg["role"], msg["content"]))
        rel = old.get("relationship", {})
        for k, v in [
            ("affection", str(rel.get("current_affection", 50))),
            ("trust_level", str(rel.get("trust_level", 0))),
            ("milestones_reached", json.dumps(rel.get("milestones_reached", []))),
            ("first_chat", old.get("conversation", {}).get("first_chat", "")),
            ("total_messages", str(old.get("conversation", {}).get("total_messages", 0))),
            ("last_message_time", old.get("time_tracking", {}).get("last_message_time", "")),
        ]:
            if v:
                c.execute("INSERT OR REPLACE INTO metadata VALUES (?,?)", (k, v))


    # ── Conflict Resolution (Mem0-inspired) ───────────────────────────────────
    def _detect_conflict(self, kind: str, new_value: str, c) -> "sqlite3.Row | None":
        """
        Tìm memory item cùng kind có thể mâu thuẫn với new_value.
        Dùng embedding similarity — nếu score > 0.82 nhưng nội dung khác → conflict.
        Fallback về keyword overlap nếu embedding không available.
        """
        if kind not in _CONFLICTABLE_KINDS:
            return None

        rows = list(c.execute(
            "SELECT id, value, embedding FROM memory_items "
            "WHERE kind=? AND superseded=0 AND value != ?",
            (kind, new_value)
        ))
        if not rows:
            return None

        new_vec = _get_ollama_embedding(new_value)

        for row in rows:
            old_value = row["value"]

            # Embedding similarity check
            if new_vec is not None and row["embedding"]:
                try:
                    old_vec = np.frombuffer(row["embedding"], dtype=np.float32)
                    sim = _cosine_similarity(new_vec, old_vec)
                    # High similarity (same topic) but different content → conflict
                    if sim > 0.82:
                        return row
                except Exception:
                    pass

            # Fallback: keyword overlap
            new_tokens = set(re.findall(r"[a-zA-ZÀ-ỹ]{3,}", new_value.lower()))
            old_tokens = set(re.findall(r"[a-zA-ZÀ-ỹ]{3,}", old_value.lower()))
            if new_tokens and old_tokens:
                overlap = len(new_tokens & old_tokens) / max(len(new_tokens), len(old_tokens))
                if overlap > 0.5:
                    return row

        return None

    def _resolve_conflict(self, kind: str, old_row, new_value: str, c, conn=None):
        """
        Xử lý conflict: archive old fact vào L3 Temporal, promote new fact.
        Log vào memory_conflicts table.
        KHÔNG acquire db_lock — caller (add_item) đã hold lock.
        """
        old_value = old_row["value"]
        now = datetime.now().isoformat()
        change_note_id = None

        # 1. Mark old item là superseded (không xóa — giữ lịch sử)
        c.execute(
            "UPDATE memory_items SET superseded=1, last_used_at=? WHERE id=?",
            (now, old_row["id"])
        )
        # 2. Lưu vào L3 Temporal như một sự kiện thay đổi
        change_note = f"[Thay đổi] {kind}: '{old_value}' → '{new_value}'"
        c.execute(
            "INSERT OR IGNORE INTO memory_items (kind,value,layer,weight,saliency,source_turn,created_at) "
            "VALUES ('episodic',?,?,1.0,4,?,?)",
            (change_note, LAYER_TEMPORAL, self.turn_counter, now)
        )
        row = c.execute(
            "SELECT id FROM memory_items WHERE kind='episodic' AND value=?",
            (change_note,)
        ).fetchone()
        if row:
            change_note_id = row["id"]
        # 3. Log conflict
        c.execute(
            "INSERT INTO memory_conflicts (kind,old_value,new_value,resolved_at,note) VALUES (?,?,?,?,?)",
            (kind, old_value, new_value, now, "auto-resolved: newer info wins")
        )
        # Không commit ở đây — caller sẽ commit sau khi INSERT item mới

        # 4. Xóa Pinecone vector cũ async (không block)
        old_id = old_row["id"]
        if old_id:
            threading.Thread(
                target=lambda: self.pinecone.delete(f"mem_{old_id}"),
                daemon=True
            ).start()
        if change_note_id and self.pinecone._enabled:
            self._upsert_pinecone_async(change_note_id, "episodic", change_note, 4)

        print(f"[Memory] Conflict resolved: {kind} '{old_value[:40]}' → '{new_value[:40]}'")

    # ── Embedding helpers ──────────────────────────────────────────────────────
    def _get_embedding(self, text: str):
        """Public embedding method — dùng Ollama, không dùng SentenceTransformer."""
        return _get_ollama_embedding(text)

    def _embed_to_blob(self, text: str) -> bytes | None:
        vec = _get_ollama_embedding(text)
        if vec is not None and np is not None:
            return vec.tobytes()
        return None

    # ── Session Memory (L2) ────────────────────────────────────────────────────
    def add_session_item(self, value: str, kind: str = "session"):
        """Thêm item vào L2 Session — chỉ tồn tại trong session hiện tại."""
        self._expire_session_memory_if_idle()
        now_iso = datetime.now().isoformat()
        self._session_items.append({
            "kind": kind, "value": value,
            "created_at": now_iso
        })
        self._session_last_activity_at = now_iso
        # Giữ tối đa 30 session items
        if len(self._session_items) > 30:
            self._session_items.pop(0)

    def update_rolling_stream_summary(self, summary: str):
        """Cập nhật tóm tắt nén của buổi stream hiện tại."""
        self._rolling_stream_summary = summary
        print(f"[Memory] Updated rolling stream summary: {summary[:50]}...")

    def clear_session_memory(self):
        """Xóa L2 Session — gọi khi stream stop hoặc sau 4h không chat."""
        count = len(self._session_items)
        self._session_items.clear()
        self._rolling_stream_summary = ""
        self._session_last_activity_at = None
        if count:
            print(f"[Memory] Cleared {count} session items and rolling summary (L2).")

    def get_session_context(self) -> str:
        """
        Trả về L2 Session context để inject vào prompt.
        Kết hợp tóm tắt nén (Rolling Summary) và các sự kiện gần nhất.
        """
        self._expire_session_memory_if_idle()
        if not self._session_items and not self._rolling_stream_summary:
            return ""
        
        parts = []
        if self._rolling_stream_summary:
            parts.append(f"[Tóm tắt stream đến giờ]\n{self._rolling_stream_summary}")
        
        if self._session_items:
            # Lấy 5 items cuối cùng để giữ độ tươi mới
            recent = [f"- {i['value']}" for i in self._session_items[-5:]]
            parts.append("[Diễn biến mới nhất]\n" + "\n".join(recent))
            
        return "[Session context (L2)]\n" + "\n\n".join(parts)

    def _expire_session_memory_if_idle(self, idle_hours: float = 4.0):
        """Auto-clear L2 session memory nếu bị bỏ quá lâu."""
        if not self._session_items or not self._session_last_activity_at:
            return
        try:
            last_activity = datetime.fromisoformat(self._session_last_activity_at)
        except Exception:
            self.clear_session_memory()
            return
        if datetime.now() - last_activity >= timedelta(hours=idle_hours):
            self.clear_session_memory()


    # ── add_item (với conflict resolution) ────────────────────────────────────
    def add_item(self, kind: str, value, weight=1.0, limit=12):
        """
        Thêm memory item.
        - Detect conflict với items cùng kind
        - Nếu conflict → resolve (archive old, promote new)
        - L3 items (episodic) → upsert lên Pinecone async
        """
        self._clear_cache()
        if not value:
            return
        text = str(value).strip()
        if not text:
            return

        layer = _LAYER_MAP.get(kind, LAYER_USER)

        key_map = {
            "like":      ("likes",     "preferences", "likes",            20),
            "dislike":   ("dislikes",  "preferences", "dislikes",         15),
            "goal":      ("goals",     "facts",       "goals",            10),
            "topic":     ("topics",    "conversation","favorite_topics",  12),
            "inside_joke":("inside_jokes","facts",    "inside_jokes",      8),
            "episodic":  ("episodic",  None,          None,               12),
            "relational":("relational",None,          None,               12),
        }
        mapping = key_map.get(kind)
        if not mapping:
            return
        bucket, section, key, bucket_limit = mapping

        # Update in-memory dict
        groups = self.memory.setdefault("memory_items", {})
        items  = groups.setdefault(bucket, [])
        if text in items:
            items.remove(text)
        items.insert(0, text)
        groups[bucket] = items[:limit]
        if section and key:
            target = self.memory[section].get(key, [])
            if text in target:
                target.remove(text)
            target.insert(0, text)
            self.memory[section][key] = target[:bucket_limit]

        # Persist to DB
        try:
            conn = self._get_db()
            if not conn:
                return
            c = conn.cursor()
            saliency   = self.estimate_saliency(kind, text)
            importance = {"goal": 1.5, "relational": 1.4, "inside_joke": 1.5, "like": 1.2, "dislike": 1.2, "episodic": 1.0, "topic": 1.0}.get(kind, 1.0)
            now        = datetime.now().isoformat()
            pinecone_row_id = None

            with self.db_lock:
                # Conflict check (chỉ cho L1 User Memory) — trong lock để atomic
                if layer == LAYER_USER:
                    conflict_row = self._detect_conflict(kind, text, c)
                    if conflict_row:
                        self._resolve_conflict(kind, conflict_row, text, c, conn)

                # Lấy embedding hiện có nếu đã có
                existing = c.execute(
                    "SELECT id, embedding FROM memory_items WHERE kind=? AND value=?",
                    (kind, text)
                ).fetchone()
                emb_blob = existing["embedding"] if existing else None

                c.execute(
                    "INSERT INTO memory_items "
                    "(kind,value,layer,weight,saliency,importance,access_count,source_turn,last_used_at,embedding) "
                    "VALUES (?,?,?,?,?,?,0,?,?,?) "
                    "ON CONFLICT(kind,value) DO UPDATE SET "
                    "weight=excluded.weight, saliency=excluded.saliency, importance=excluded.importance, "
                    "layer=excluded.layer, last_used_at=excluded.last_used_at, embedding=excluded.embedding, "
                    "superseded=0",
                    (kind, text, layer, weight, saliency, importance, self.turn_counter, now, emb_blob)
                )
                conn.commit()

                # Lấy id sau khi commit (cursor vẫn valid trong lock)
                if layer == LAYER_TEMPORAL and self.pinecone._enabled:
                    row = c.execute(
                        "SELECT id FROM memory_items WHERE kind=? AND value=?", (kind, text)
                    ).fetchone()
                    if row:
                        pinecone_row_id = row["id"]

            # Pinecone upsert ngoài lock (non-blocking)
            if pinecone_row_id is not None:
                self._upsert_pinecone_async(pinecone_row_id, kind, text, saliency)

        except Exception as e:
            print(f"[Memory] add_item DB error: {e}")

        self._is_dirty = True

    def _upsert_pinecone_async(self, item_id: int, kind: str, text: str, saliency: float):
        """Upsert L3 item lên Pinecone trong background thread."""
        def _run():
            vec = _get_ollama_embedding(text)
            if vec is None:
                return
            self.pinecone.upsert(
                item_id=f"mem_{item_id}",
                vector=vec.tolist(),
                metadata={"kind": kind, "value": text[:500], "saliency": saliency}
            )
            # Lưu pinecone_id vào DB
            try:
                conn = self._get_db()
                if conn:
                    with self.db_lock:
                        conn.execute(
                            "UPDATE memory_items SET pinecone_id=?, embedding=? WHERE id=?",
                            (f"mem_{item_id}", vec.tobytes(), item_id)
                        )
                        conn.commit()
            except Exception:
                pass
        threading.Thread(target=_run, daemon=True).start()


    # ── Save ───────────────────────────────────────────────────────────────────
    def save(self):
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
                    c.execute("INSERT OR REPLACE INTO profile VALUES (?,?)", (k, str(v)))

            for k, v in self.memory.get("identity", {}).items():
                if v:
                    c.execute("INSERT OR REPLACE INTO self_profile VALUES (?,?)", (k, str(v)))

            for item in self.memory["preferences"].get("likes", []):
                c.execute("INSERT OR IGNORE INTO preferences (type,value) VALUES ('like',?)", (item,))
            for item in self.memory["preferences"].get("dislikes", []):
                c.execute("INSERT OR IGNORE INTO preferences (type,value) VALUES ('dislike',?)", (item,))
            for item in self.memory["facts"].get("goals", []):
                c.execute("INSERT OR IGNORE INTO facts (type,value) VALUES ('goal',?)", (item,))
            for item in self.memory["conversation"].get("favorite_topics", []):
                c.execute("INSERT OR IGNORE INTO facts (type,value) VALUES ('topic',?)", (item,))

            weighted_groups = [
                ("like",      self.memory.get("memory_items", {}).get("likes", []),     1.0, LAYER_USER),
                ("dislike",   self.memory.get("memory_items", {}).get("dislikes", []),  1.0, LAYER_USER),
                ("goal",      self.memory.get("memory_items", {}).get("goals", []),     1.4, LAYER_USER),
                ("topic",     self.memory.get("memory_items", {}).get("topics", []),    1.1, LAYER_USER),
                ("inside_joke", self.memory.get("facts", {}).get("inside_jokes", []),   1.5, LAYER_USER),
                ("episodic",  self.memory.get("memory_items", {}).get("episodic", []),  1.2, LAYER_TEMPORAL),
                ("relational",self.memory.get("memory_items", {}).get("relational", []),1.3, LAYER_USER),
            ]
            for kind, values, weight, layer in weighted_groups:
                importance = {"goal": 1.5, "relational": 1.4, "inside_joke": 1.5, "like": 1.2, "dislike": 1.2, "episodic": 1.0, "topic": 1.0}.get(kind, 1.0)
                for value in values[:20]:
                    if not value:
                        continue
                    saliency = self.estimate_saliency(kind, value)
                    existing = c.execute(
                        "SELECT id, embedding FROM memory_items WHERE kind=? AND value=?", (kind, str(value))
                    ).fetchone()
                    emb_blob = existing["embedding"] if existing else None
                    c.execute(
                        "INSERT INTO memory_items "
                        "(kind,value,layer,weight,saliency,importance,access_count,source_turn,last_used_at,embedding) "
                        "VALUES (?,?,?,?,?,?,0,?,?,?) "
                        "ON CONFLICT(kind,value) DO UPDATE SET "
                        "weight=excluded.weight, saliency=excluded.saliency, importance=excluded.importance, "
                        "layer=excluded.layer, last_used_at=excluded.last_used_at",
                        (kind, str(value), layer, weight, saliency, importance, self.turn_counter, now, emb_blob)
                    )

            self.memory["conversation"]["conversation_thread"] = self.memory["conversation"]["conversation_thread"][-40:]
            c.execute("DELETE FROM conversation")
            for msg in self.memory["conversation"]["conversation_thread"]:
                if isinstance(msg, dict) and msg.get("role"):
                    c.execute("INSERT INTO conversation (role,content) VALUES (?,?)", (msg["role"], msg.get("content", "")))

            for k, v in [
                ("affection",          str(self.memory["relationship"].get("current_affection", 50))),
                ("trust_level",        str(self.memory["relationship"].get("trust_level", 0))),
                ("milestones_reached", json.dumps(self.memory["relationship"].get("milestones_reached", []))),
                ("last_chat",          now),
                ("last_message_time",  now),
                ("total_messages",     str(self.turn_counter)),
                ("conversation_count", str(self.memory["conversation"].get("conversation_count", 0))),
                ("memory_buffer",      json.dumps(self.memory_buffer, ensure_ascii=False)),
            ]:
                c.execute("INSERT OR REPLACE INTO metadata VALUES (?,?)", (k, v))

            if not self.memory["conversation"].get("first_chat"):
                c.execute("INSERT OR IGNORE INTO metadata VALUES ('first_chat',?)", (now,))

            conn.commit()
        self._is_dirty = False

    def _clear_cache(self):
        self._basic_context_cache  = None
        self._rag_context_cache    = None
        self._rag_cache_key        = None
        self._relevant_items_cache = None


    def get_context(self, user_input: str, intent: str = None, is_public: bool = False):
        """
        Build prompt context (L1 profiling + L2 session + L3 Temporal RAG).
        Nếu is_public=True (Stream mode), sẽ lọc bỏ các thông tin nhạy cảm.
        Đồng thời 'touch' (update access_count) cho các memory được inject để tránh bị xóa nhầm.
        """
        # Tránh dùng cache nếu role (public/private) thay đổi
        cache_key = f"basic_{is_public}"
        if hasattr(self, "_last_context_role") and self._last_context_role != is_public:
            self._basic_context_cache = None
        self._last_context_role = is_public

        if self._basic_context_cache is not None:
            return self._basic_context_cache

        full = self.memory
        
        # 1. Thu thập danh sách (kind, value) để 'touch'
        to_touch = []
        
        # L1 Profile
        up = full.get("user_profile", {})
        for k, v in up.items():
            if v: to_touch.append(("profile", v))
        
        # Identity (Lyra's own info)
        ident = full.get("identity", {})
        for k, v in ident.items():
             if v: to_touch.append(("identity", v))

        # L1 Facts/Preferences
        for k in ["likes", "dislikes"]:
            for v in full.get("preferences", {}).get(k, []):
                to_touch.append((k.rstrip('s'), v))
        
        for k in ["topics", "goals", "inside_jokes"]:
            for v in full.get("facts", {}).get(k, []):
                to_touch.append((k.rstrip('s'), v))

        # Thực hiện touch ngầm (background) để ko cản trở build prompt
        if to_touch:
            threading.Thread(target=self.touch_items, args=(to_touch,), daemon=True).start()

        # 2. Build parts
        try:
            profile = self.memory.get("user_profile", {})
            prefs   = self.memory.get("preferences", {})
            facts   = self.memory.get("facts", {})
            topics  = self.memory.get("conversation", {}).get("favorite_topics", []) or facts.get("topics", [])
            parts   = []

            profile_bits = []
            # LỌC THÔNG TIN: Nếu đang public, có thể ẩn đi các thông tin quá chi tiết
            if profile.get("name"):       profile_bits.append(profile["name"])
            if profile.get("age_range"):  profile_bits.append(profile["age_range"])
            
            if not is_public:
                if profile.get("occupation"): profile_bits.append(profile["occupation"])
                if profile.get("location"):   profile_bits.append(f"from {profile['location']}")
            
            if profile_bits:
                parts.append("They are: " + ", ".join(profile_bits))

            if prefs.get("likes"):    parts.append("Likes: "    + ", ".join(prefs["likes"][:6]))
            if prefs.get("dislikes"): parts.append("Dislikes: " + ", ".join(prefs["dislikes"][:4]))

            if topics:              parts.append("Into: "  + ", ".join(topics[:6]))
            if facts.get("goals"):  parts.append("Goals: " + ", ".join(facts["goals"][:3]))

            # Inside Jokes and Relational bond
            jokes = facts.get("inside_jokes", [])
            if jokes: parts.append("Jokes: " + ", ".join(jokes[:3]))
            
            # Chỉ hiện Bond (affection/notes) ở private chat
            if not is_public:
                rel = self.memory.get("memory_items", {}).get("relational", [])
                if rel: parts.append("Bond: " + ", ".join(rel[:4]))

            if not parts:
                self._basic_context_cache = ""
                return ""
            
            header = "What you know about them:\n" if not is_public else "Stream Context (Viewer/Creator Info):\n"
            self._basic_context_cache = header + "\n".join(f"- {p}" for p in parts)
            return self._basic_context_cache
        except Exception as e:
            print(f"[Memory] get_context error: {e}")
            return ""

    def get_relevant_context(self, user_input: str, is_public: bool = False) -> str:
        """
        Unified Ranking Pipeline:
        1. Gathers candidates from L1 (Facts), L2 (Session), L3 (Temporal).
        2. Filter out creator-private if is_public=True.
        3. Use MemoryRanker (Model-based) to select top relevant context fitting ~550 tokens.
        """
        cache_key = f"ranked_{self.turn_counter}_{is_public}_{user_input.strip().lower()}"
        if self._rag_context_cache and cache_key == self._rag_cache_key:
            return self._rag_context_cache

        candidates = []

        # ── Collect L1 Candidates (User/Shared) ────────────────────────────────
        try:
            conn = self._get_db()
            if conn:
                c = conn.cursor()
                # Fetch more than we need for the ranker to choose
                rows = list(c.execute(
                    "SELECT kind, value, weight, saliency FROM memory_items "
                    "WHERE layer=? AND superseded=0 "
                    "ORDER BY saliency DESC, created_at DESC LIMIT 50",
                    (LAYER_USER,)
                ))
                for r in rows:
                    kind = r["kind"]
                    # Privacy filter
                    if is_public and kind in ("profile", "relational"):
                        continue
                    candidates.append({
                        "kind": kind,
                        "value": r["value"],
                        "weight": r["weight"] * (1.0 + (r["saliency"] / 10.0))
                    })
        except Exception:
            pass

        # ── Collect L2 Candidates (Session) ────────────────────────────────────
        for item in self._session_items[-10:]:
            candidates.append({
                "kind": "session",
                "value": item["value"],
                "weight": 1.2 # Recent session events are highly relevant
            })

        # ── Collect L3 Candidates (Temporal RAG) ──────────────────────────────
        query_vec = self._get_embedding(user_input)
        if query_vec is not None and self.pinecone._enabled:
            try:
                matches = self.pinecone.query(query_vec.tolist(), top_k=6)
                for m in matches:
                    if m["score"] > 0.65:
                        val = m["metadata"].get("value", "")
                        if val:
                            candidates.append({
                                "kind": "temporal",
                                "value": val,
                                "weight": m["score"] * 1.5 # Boost RAG matches
                            })
            except Exception:
                pass

        # ── Ranking ───────────────────────────────────────────────────────────
        context_items = self.ranker.rank(user_input, candidates, token_budget=550)

        if not context_items:
            self._rag_cache_key = cache_key
            self._rag_context_cache = ""
            return ""

        # P1.1: Touch items to keep them from being forgotten
        to_touch = []
        for text in context_items:
            # We don't have the original 'kind' here easily, but we can search for the item
            # To simplify, we only touch items that are in the original candidates list
            for cand in candidates:
                if cand["value"] == text:
                    to_touch.append((cand["kind"], text))
                    break
        
        if to_touch:
            threading.Thread(target=self.touch_items, args=(to_touch,), daemon=True).start()

        result = "Bối cảnh quan trọng:\n" + "\n".join(f"- {m}" for m in context_items)
        self._rag_cache_key = cache_key
        self._rag_context_cache = result
        return result

    def get_focused_context(self, user_input=""):
        """Compact context — chỉ name + 1 episodic gần nhất."""
        try:
            profile  = self.memory.get("user_profile", {})
            episodic = self.memory.get("memory_items", {}).get("episodic", [])
            parts    = []
            if profile.get("name"):
                parts.append(f"Name: {profile['name']}")
            if episodic:
                parts.append(f"Recent: {episodic[0][:60]}")
            return "\n".join(parts) if parts else ""
        except Exception as e:
            print(f"[Memory] get_focused_context error: {e}")
            return ""

    def _tokenize(self, text):
        if not text:
            return set()
        return {t for t in re.findall(r"[a-zA-Z0-9']+", text.lower()) if len(t) >= 3}


    # ── Saliency ───────────────────────────────────────────────────────────────
    def estimate_saliency(self, kind, value):
        text  = str(value or "").lower()
        score = 1
        if kind == "goal":      score += 4
        elif kind == "relational": score += 4
        elif kind == "episodic":   score += 2
        elif kind in ("dislike", "like", "topic"): score += 1
        strong = ["stress","stressed","sad","scared","afraid","angry","hurt","love","hate",
                  "important","finally","proud","fail","failed","exam","deadline","lonely",
                  "miss","anxious","panic"]
        if any(w in text for w in strong): score += 3
        if len(text.split()) >= 8:         score += 1
        return max(1, min(10, score))

    # ── Buffer / extraction helpers ────────────────────────────────────────────
    def buffer_candidate(self, kind, value, saliency=None):
        text = str(value or "").strip()
        if not text:
            return
        if saliency is None:
            saliency = self.estimate_saliency(kind, text)
        for item in self.memory_buffer:
            if item.get("kind") == kind and item.get("value", "").lower() == text.lower():
                item["saliency"] = max(item.get("saliency", 0), saliency)
                item["count"]    = item.get("count", 1) + 1
                item["last_seen_turn"] = self.turn_counter
                self.memory["memory_buffer"] = self.memory_buffer
                return
        self.memory_buffer.append({
            "kind": kind, "value": text[:160], "saliency": saliency,
            "count": 1, "last_seen_turn": self.turn_counter,
        })
        if len(self.memory_buffer) > 24:
            self.memory_buffer = self.memory_buffer[-24:]
        self.memory["memory_buffer"] = self.memory_buffer

    def should_buffer(self, text, intent=None):
        cleaned = (text or "").strip()
        if len(cleaned) < 8:
            return False
        lowered = cleaned.lower()
        keywords = ["i like","i love","i hate","my goal","i want to","i need to",
                    "i'm trying to","i am trying to","i feel","i felt","i was","remember",
                    "i have","my exam","deadline","project","school","work","stress",
                    "stressed","anxious","sad","proud","finally"]
        if any(k in lowered for k in keywords):
            return True
        if intent in ("introduction", "complaint", "request"):
            return True
        return len(cleaned.split()) >= 14

    def extract_candidates_heuristic(self, text):
        cleaned  = (text or "").strip()
        lowered  = cleaned.lower()
        candidates = []

        def add(kind, value):
            value = (value or "").strip(" .,!?\n\t")
            if value:
                candidates.append({"kind": kind, "value": value[:160], "saliency": self.estimate_saliency(kind, value)})

        for p in [r"(?:i like|i love|i'm into|i am into)\s+(.+)"]:
            m = re.search(p, lowered)
            if m: add("like", m.group(1)); break
        for p in [r"(?:i hate|i dislike|i can't stand)\s+(.+)"]:
            m = re.search(p, lowered)
            if m: add("dislike", m.group(1)); break
        for p in [r"(?:i want to|i need to|i'm trying to|i am trying to|my goal is to)\s+(.+)"]:
            m = re.search(p, lowered)
            if m: add("goal", m.group(1)); break

        for kw in ["math","code","coding","python","exam","study","school","work","project"]:
            if kw in lowered:
                add("topic", kw)

        if any(w in lowered for w in ["stressed","sad","anxious","deadline","exam","proud","finally"]):
            add("episodic", cleaned)

        seen   = set()
        unique = []
        for item in candidates:
            key = (item["kind"], item["value"].lower())
            if key not in seen:
                seen.add(key)
                unique.append(item)
        return unique[:5]

    def should_flush(self, intent=None):
        if not self.memory_buffer:
            return False
        if len(self.memory_buffer) >= 6:
            return True
        if any(item.get("saliency", 0) >= 7 for item in self.memory_buffer):
            return True
        return self.turn_counter % 6 == 0 and len(self.memory_buffer) >= 3

    def flush_buffer(self, recent_messages, user_input=""):
        if not self.memory_buffer:
            return []
        candidates = [
            {"kind": i.get("kind"), "value": i.get("value"), "saliency": i.get("saliency", 0), "count": i.get("count", 1)}
            for i in self.memory_buffer[-8:]
        ]
        convo_snippet = ""
        for msg in recent_messages[-6:]:
            if isinstance(msg, dict) and "role" in msg and "content" in msg:
                role = "User" if msg["role"] == "user" else "Lyra"
                convo_snippet += f"{role}: {msg['content']}\n"
        if user_input:
            convo_snippet += f"User: {user_input}\n"
        try:
            from config import GROQ_API_KEY, TRANSLATE_BASE_URL, TRANSLATE_MODEL
            resp = requests.post(
                TRANSLATE_BASE_URL,
                headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
                json={
                    "model": TRANSLATE_MODEL,
                    "messages": [
                        {"role": "system", "content": (
                            "You are a memory editor. Given rough candidate memories from a recent chat, "
                            "keep only what is worth remembering later. Drop trivia and duplicates. "
                            "Return ONLY JSON: {\"memories\":[{\"kind\":\"goal|topic|like|dislike|episodic|relational\","
                            "\"value\":\"short memory\",\"saliency\":1-10}]}. Keep at most 4 memories."
                        )},
                        {"role": "user", "content": f"Recent chat:\n{convo_snippet}\nCandidates:\n{json.dumps(candidates, ensure_ascii=False)}"},
                    ],
                    "temperature": 0.1, "max_tokens": 220,
                },
                timeout=15, verify=False,
            )
            raw = resp.json().get("choices", [{}])[0].get("message", {}).get("content", "").strip()
            raw = re.sub(r"```json|```", "", raw).strip()
            kept = json.loads(raw).get("memories", []) if raw else []
        except Exception as e:
            print(f"[Memory] flush failed: {e}")
            kept = [i for i in candidates if i.get("saliency", 0) >= 6][:3]

        for item in kept:
            if item.get("kind") and item.get("value"):
                self.add_item(item["kind"], item["value"], weight=1.0, limit=12)

        self.memory_buffer.clear()
        self.memory["memory_buffer"] = self.memory_buffer
        return kept


    # ── Touch / Consolidate ────────────────────────────────────────────────────
    def touch_items(self, items):
        if not items:
            return
        conn = self._get_db()
        if not conn:
            return
        c   = conn.cursor()
        now = datetime.now().isoformat()
        with self.db_lock:
            for kind, value in items:
                if kind and value:
                    c.execute(
                        "UPDATE memory_items SET last_used_at=?, access_count=MIN(access_count+1,10000) "
                        "WHERE kind=? AND value=?",
                        (now, kind, value)
                    )
            conn.commit()

    def get_rare_memory(self) -> str:
        """
        Retrieves a 'rare' memory: highly salient but rarely accessed.
        Used for the Unpredictable Reward (Dopamine) system.
        """
        conn = self._get_db()
        if not conn:
            return ""
            
        try:
            with self.db_lock:
                # Threshold >= 3 để bao gồm goal/relational (saliency 5) và
                # like/dislike có strong word (saliency 4+).
                # Threshold >= 6 quá cao — hầu hết items thông thường không đạt.
                query = """
                    SELECT value FROM memory_items 
                    WHERE kind IN ('like', 'dislike', 'goal', 'episodic', 'relational', 'inside_joke')
                    AND saliency >= 3
                    ORDER BY access_count ASC, saliency DESC
                    LIMIT 5
                """
                res = conn.execute(query).fetchall()
                if res:
                    # random.choice trong top 5 ít-access nhất — vẫn có yếu tố bất ngờ
                    return random.choice(res)[0]
        except Exception as e:
            print(f"[Memory] get_rare_memory error: {e}")
            
        return ""

    def consolidate_episodic_to_semantic(self):
        """
        Complementary Learning System (CLS) - Post-stream consolidation.
        Distills today's episodic events into permanent L1 facts.
        """
        try:
            from cls_consolidator import CLSConsolidator
            consolidator = CLSConsolidator()
            
            # 1. Fetch today's episodes (last 16h)
            conn = self._get_db()
            if not conn: return
            
            cutoff = (datetime.now() - timedelta(hours=16)).isoformat()
            rows = conn.execute(
                "SELECT value FROM memory_items WHERE kind='episodic' AND created_at > ? AND superseded=0",
                (cutoff,)
            ).fetchall()
            
            episodes = [r["value"] for r in rows]
            if not episodes:
                print("[CLS] No new episodes to consolidate.")
                return

            # 2. Get current semantic context for the AI to compare
            current_facts = {
                "likes": self.memory.get("preferences", {}).get("likes", [])[:10],
                "goals": self.memory.get("facts", {}).get("goals", [])[:5]
            }

            # 3. Distill
            print(f"[CLS] Consolidating {len(episodes)} episodes...")
            new_facts = consolidator.distill_episodic_memories(episodes, current_facts)
            
            # 4. Integrate into DB
            if new_facts:
                for item in new_facts:
                    kind = item.get("kind")
                    val  = item.get("value")
                    if kind and val:
                        self.add_item(kind, val, weight=1.1)
                print(f"[CLS] Integrated {len(new_facts)} new semantic facts.")

            # 5. Personality shifts (optional, hidden in DB for now)
            summary = self._rolling_stream_summary
            shifts = consolidator.update_personality_indices(episodes, summary)
            if shifts:
                # Update mood bias or other indicators in metadata
                with self.db_lock:
                    for key, val in shifts.items():
                        conn.execute("INSERT OR REPLACE INTO metadata VALUES (?,?)", (f"idx_{key}", str(val)))
                    conn.commit()
                print(f"[CLS] Personality indices updated: {shifts}")

        except Exception as e:
            print(f"[CLS] Consolidation failed: {e}")

    def consolidate(self):
        """Xóa L1 items stale. L3 items không bị xóa (temporal = lịch sử)."""
        try:
            conn = self._get_db()
            if not conn:
                return
            c = conn.cursor()
            with self.db_lock:
                c.execute(
                    "DELETE FROM memory_items "
                    "WHERE layer=? AND access_count=0 "
                    "AND (? - source_turn) > 100 "
                    "AND saliency < 7 AND COALESCE(importance,1.0) < 1.3",
                    (LAYER_USER, self.turn_counter)
                )
                deleted = c.rowcount
                if deleted:
                    print(f"[Memory] Forgot {deleted} stale L1 items.")
                conn.commit()
        except Exception as e:
            print(f"[Memory] consolidate error: {e}")

    # ── Stream milestones ──────────────────────────────────────────────────────
    def check_stream_milestone(self, event_type: str, description: str, stream_title: str = "", peak_viewers: int = 0) -> bool:
        try:
            conn = self._get_db()
            if not conn:
                return False
            c   = conn.cursor()
            now = datetime.now().isoformat()
            with self.db_lock:
                if c.execute("SELECT id FROM stream_milestones WHERE event_type=?", (event_type,)).fetchone():
                    return False
                c.execute(
                    "INSERT INTO stream_milestones (event_type,description,achieved_at,stream_title,peak_viewers) VALUES (?,?,?,?,?)",
                    (event_type, description, now, stream_title, peak_viewers)
                )
                conn.commit()
                print(f"[Memory] New stream milestone: {event_type} — {description}")
                return True
        except Exception as e:
            print(f"[Memory] check_stream_milestone error: {e}")
            return False

    def get_stream_count(self) -> int:
        """Đếm số lượng buổi stream đã lưu (thread-safe).
        Dùng metadata key 'stream_session_count' — tăng mỗi lần stream/stop.
        """
        try:
            conn = self._get_db()
            if not conn: return 0
            with self.db_lock:
                row = conn.execute("SELECT value FROM metadata WHERE key='stream_session_count'").fetchone()
                return int(row[0]) if row else 0
        except Exception:
            return 0

    def increment_stream_count(self) -> int:
        """Tăng stream session count lên 1 và trả về giá trị mới."""
        try:
            conn = self._get_db()
            if not conn: return 0
            with self.db_lock:
                row = conn.execute("SELECT value FROM metadata WHERE key='stream_session_count'").fetchone()
                new_count = (int(row[0]) if row else 0) + 1
                conn.execute("INSERT OR REPLACE INTO metadata VALUES ('stream_session_count', ?)", (str(new_count),))
                conn.commit()
                return new_count
        except Exception as e:
            print(f"[Memory] increment_stream_count error: {e}")
            return 0

    def get_stream_milestones(self, limit: int = 5) -> list:
        try:
            conn = self._get_db()
            if not conn:
                return []
            rows = conn.execute(
                "SELECT event_type, description, achieved_at, stream_title FROM stream_milestones ORDER BY id DESC LIMIT ?",
                (limit,)
            ).fetchall()
            return [{"event_type": r["event_type"], "description": r["description"],
                     "achieved_at": r["achieved_at"], "stream_title": r["stream_title"]} for r in rows]
        except Exception as e:
            print(f"[Memory] get_stream_milestones error: {e}")
            return []

    # ── Secret Diary ──────────────────────────────────────────────────────────
    def add_diary_entry(self, content: str, mood: float = 0.0, affection: float = 50.0):
        try:
            conn = self._get_db()
            if not conn:
                return
            with self.db_lock:
                conn.execute(
                    "INSERT INTO diaries (content, mood_score, affection_score) VALUES (?,?,?)",
                    (content, mood, affection)
                )
                conn.commit()
                print(f"[Memory] New diary entry saved.")
        except Exception as e:
            print(f"[Memory] add_diary_entry error: {e}")

    def get_diary_entries(self, limit: int = 10) -> list:
        try:
            conn = self._get_db()
            if not conn:
                return []
            rows = conn.execute(
                "SELECT id, content, mood_score, affection_score, timestamp FROM diaries ORDER BY id DESC LIMIT ?",
                (limit,)
            ).fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            print(f"[Memory] get_diary_entries error: {e}")
            return []

    # ── In-memory fallback (khi DB locked) ────────────────────────────────────
    def _get_in_memory_fallback(self):
        class _FallbackDB:
            def cursor(self): return _FallbackCursor()
            def commit(self): pass
            def execute(self, sql, *a): pass
            def close(self): pass
        class _FallbackCursor:
            def execute(self, sql, params=None): return self
            def executescript(self, sql): pass
            def fetchone(self): return None
            def fetchall(self): return []
            def __iter__(self): return iter([])
            @property
            def rowcount(self): return 0
        if not hasattr(self, "_fallback_conn"):
            self._fallback_conn = _FallbackDB()
            print("[Memory] Using in-memory fallback mode")
        return self._fallback_conn

    def set_self_info(self, key: str, value: str):
        """Lưu thông tin bản diện của LYRA (Identity)"""
        if not value: return
        if "identity" not in self.memory: self.memory["identity"] = {}
        self.memory["identity"][key] = value
        self._is_dirty = True
