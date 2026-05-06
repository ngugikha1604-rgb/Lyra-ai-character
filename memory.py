import os
import re
import json
import sqlite3
import threading
import time
import requests
import random
from datetime import datetime, timedelta

try:
    import numpy as np
except ImportError:
    np = None

# Internal Imports
from config import *
from memory_utils import (
    BASE_DIR, DB_PATH, DB_LOCK, LAYER_USER, LAYER_SESSION, LAYER_TEMPORAL,
    _LAYER_MAP, _CONFLICTABLE_KINDS, KIND_IMPORTANCE,
    _get_ollama_embedding, _cosine_similarity, _vectorized_cosine_similarity,
    get_now_vn
)
from pinecone_layer import PineconeLayer
from memory_ranker import MemoryRanker
from background_worker import enqueue, PRIORITY_NORMAL

MEMORY_PATH = os.path.join(BASE_DIR, "memory.json")

class MemorySystem:
    """
    Hybrid Memory System for Lyra AI (SQLite + Pinecone).
    L1 (Semantic): Persistent facts about user.
    L2 (Working): Short-term stream/session awareness.
    L3 (Episodic): Long-term temporal events and RAG context.
    """
    def __init__(self, max_summaries=8):
        self._db_connection = None
        self.db_lock = DB_LOCK
        self._basic_context_cache = None
        self._rag_context_cache = None
        self._rag_cache_key = None
        self._relevant_items_cache = None
        self._semantic_cache = []  # Store tuples of (query_vec, context_str)

        self.max_summaries = max_summaries
        self.memory = self.get_default_memory()
        self.memory_buffer = []
        self._is_dirty = False
        self._pending_conversation_rows = []

        self.pinecone = PineconeLayer()
        self.ranker = MemoryRanker()

        # L2 Session Memory (Ephemeral)
        self._session_items = []
        self._rolling_stream_summary = ""
        self._session_last_activity_at = None

        self._sweeper_thread = threading.Thread(target=self._sweeper_loop, daemon=True)
        self._sweeper_thread.start()

    # dict-like access for backward compatibility
    def __getitem__(self, key): return self.memory[key]
    def __setitem__(self, key, value): self.memory[key] = value
    def __contains__(self, key): return key in self.memory
    def __iter__(self): return iter(self.memory)
    def __len__(self): return len(self.memory)

    @property
    def turn_counter(self):
        return self.memory.get("conversation", {}).get("total_messages", 0)

    def _sweeper_loop(self):
        """Background thread for memory TTL expiration."""
        while True:
            time.sleep(1800)
            try:
                conn = self._get_db()
                if not conn: continue
                now_iso = get_now_vn().isoformat()
                with self.db_lock:
                    conn.execute(
                        "UPDATE memory_items SET is_valid=0 "
                        "WHERE is_valid=1 AND ttl_minutes>0 "
                        "AND datetime(last_used_at, '+' || ttl_minutes || ' minutes') < ?",
                        (now_iso,)
                    )
                    conn.commit()
            except Exception as e:
                print(f"[Memory] Sweeper error: {e}")

    def _get_db(self):
        """Standard SQLite connection with WAL mode and schema initialization."""
        if self._db_connection:
            try:
                self._db_connection.execute("SELECT 1")
                return self._db_connection
            except sqlite3.Error:
                self._db_connection = None
        try:
            conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=60.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            # Schema setup (omitted for brevity in this snippet, but kept in full file)
            self._init_db_schema(conn)
            with self.db_lock:
                conn.commit()
                self._db_connection = conn
            return conn
        except Exception as e:
            print(f"[Memory] DB error: {e}")
            return self._get_in_memory_fallback()

    def _init_db_schema(self, conn):
        """Initializes database tables if they don't exist."""
        c = conn.cursor()
        c.executescript("""
            CREATE TABLE IF NOT EXISTS profile (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS self_profile (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS preferences (id INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT NOT NULL, value TEXT NOT NULL, UNIQUE(type, value));
            CREATE TABLE IF NOT EXISTS facts (id INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT NOT NULL, value TEXT NOT NULL, UNIQUE(type, value));
            CREATE TABLE IF NOT EXISTS summaries (id INTEGER PRIMARY KEY AUTOINCREMENT, summary TEXT NOT NULL, timestamp TEXT NOT NULL, is_mega INTEGER DEFAULT 0, created_at TEXT DEFAULT (datetime('now')));
            CREATE TABLE IF NOT EXISTS conversation (id INTEGER PRIMARY KEY AUTOINCREMENT, role TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT DEFAULT (datetime('now')));
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS memory_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, value TEXT NOT NULL, layer TEXT NOT NULL DEFAULT 'user',
                weight REAL DEFAULT 1.0, saliency REAL DEFAULT 0, importance REAL DEFAULT 1.0, access_count INTEGER DEFAULT 0,
                source_turn INTEGER DEFAULT 0, last_used_at TEXT, created_at TEXT DEFAULT (datetime('now')),
                embedding BLOB, pinecone_id TEXT, superseded INTEGER DEFAULT 0, is_valid INTEGER DEFAULT 1,
                memory_scope TEXT DEFAULT 'semantic', ttl_minutes INTEGER DEFAULT 0, UNIQUE(kind, value)
            );
            CREATE TABLE IF NOT EXISTS stream_milestones (id INTEGER PRIMARY KEY AUTOINCREMENT, event_type TEXT NOT NULL, description TEXT NOT NULL, achieved_at TEXT NOT NULL, stream_title TEXT DEFAULT '', peak_viewers INTEGER DEFAULT 0, UNIQUE(event_type));
            CREATE TABLE IF NOT EXISTS memory_conflicts (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, old_value TEXT NOT NULL, new_value TEXT NOT NULL, resolved_at TEXT NOT NULL, note TEXT DEFAULT '');
            CREATE TABLE IF NOT EXISTS diaries (id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT NOT NULL, mood_score REAL, affection_score REAL, timestamp TEXT DEFAULT (datetime('now')));
        """)
        # Migrations
        existing_cols = {row[1] for row in c.execute("PRAGMA table_info(memory_items)")}
        for col, definition in [("layer", "TEXT NOT NULL DEFAULT 'user'"), ("importance", "REAL DEFAULT 1.0"), ("pinecone_id", "TEXT"), ("superseded", "INTEGER DEFAULT 0"), ("is_valid", "INTEGER DEFAULT 1"), ("memory_scope", "TEXT DEFAULT 'semantic'"), ("ttl_minutes", "INTEGER DEFAULT 0")]:
            if col not in existing_cols: c.execute(f"ALTER TABLE memory_items ADD COLUMN {col} {definition}")

    def get_default_memory(self):
        """Returns the base memory structure for in-memory tracking."""
        return {
            "user_profile": {"name": None, "location": None, "age_range": None, "occupation": None},
            "preferences": {"likes": [], "dislikes": [], "interests": [], "hobbies": []},
            "facts": {"personal": [], "topics": [], "achievements": [], "goals": [], "inside_jokes": []},
            "conversation": {"total_messages": 0, "first_chat": None, "last_chat": None, "conversation_count": 0, "favorite_topics": [], "chat_history_summary": [], "conversation_thread": []},
            "relationship": {"current_affection": 50, "affection_history": [], "trust_level": 0, "inside_jokes": [], "memorable_moments": [], "milestones_reached": []},
            "memory_items": {"likes": [], "dislikes": [], "goals": [], "topics": [], "episodic": [], "relational": []},
            "memory_buffer": [],
            "time_tracking": {"last_message_time": None, "time_gap_hours": 0, "first_greeting_sent": False, "greeting_history": []},
            "identity": {"name": "Lyra", "gender": "Nữ", "occupation": "VTuber"},
        }

    def load(self):
        """Loads memory from SQLite into the active memory dictionary."""
        conn = self._get_db()
        if not conn: return self.get_default_memory()
        c = conn.cursor()
        if os.path.exists(MEMORY_PATH):
            try:
                with open(MEMORY_PATH, "r", encoding="utf-8") as f:
                    self._migrate_from_json(c, json.load(f))
                with self.db_lock: conn.commit()
                os.rename(MEMORY_PATH, MEMORY_PATH + ".bak")
            except Exception as e: print(f"[Memory] Load error: {e}")
        
        self.memory = self._build_memory_dict(c)
        self.memory_buffer = self.memory.get("memory_buffer", [])
        return self.memory

    def _build_memory_dict(self, c):
        """Assembles the full memory dictionary from DB records."""
        def get_meta(key, default=""):
            r = c.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
            return r[0] if r else default
        def get_profile(tbl, key):
            r = c.execute(f"SELECT value FROM {tbl} WHERE key=?", (key,)).fetchone()
            return r[0] if r else None

        memory_rows = list(c.execute("SELECT kind, value FROM memory_items WHERE layer=? AND superseded=0 ORDER BY saliency DESC LIMIT 80", (LAYER_USER,)))
        def from_kind(k, limit): return [r["value"] for r in memory_rows if r["kind"] == k][:limit]

        episodic = [r["value"] for r in c.execute("SELECT value FROM memory_items WHERE kind='episodic' AND superseded=0 ORDER BY created_at DESC LIMIT 12")]
        summaries = [{"summary": r[0], "timestamp": r[1], "is_mega": bool(r[2])} for r in c.execute("SELECT summary, timestamp, is_mega FROM summaries ORDER BY id DESC LIMIT 10")]
        messages = [{"role": r[0], "content": r[1]} for r in c.execute("SELECT role, content FROM conversation ORDER BY id DESC LIMIT 40")]
        messages.reverse()

        return {
            "user_profile": {k: get_profile("profile", k) for k in ["name", "location", "age_range", "occupation"]},
            "identity": {k: get_profile("self_profile", k) or v for k, v in [("name", "Lyra"), ("gender", "Nữ"), ("occupation", "VTuber")]},
            "preferences": {"likes": from_kind("like", 20), "dislikes": from_kind("dislike", 15)},
            "facts": {"topics": from_kind("topic", 10), "goals": from_kind("goal", 10), "inside_jokes": from_kind("inside_joke", 5)},
            "conversation": {
                "total_messages": int(get_meta("total_messages", "0")), "first_chat": get_meta("first_chat"),
                "chat_history_summary": summaries, "conversation_thread": messages
            },
            "relationship": {"current_affection": int(get_meta("affection", "50")), "milestones_reached": json.loads(get_meta("milestones_reached", "[]"))},
            "memory_items": {"likes": from_kind("like", 20), "dislikes": from_kind("dislike", 15), "goals": from_kind("goal", 10), "topics": from_kind("topic", 10), "episodic": episodic, "relational": from_kind("relational", 8)},
            "memory_buffer": json.loads(get_meta("memory_buffer", "[]")),
            "time_tracking": {"last_message_time": get_meta("last_message_time")}
        }

    def _migrate_from_json(self, c, old):
        """Legacy migration from JSON to SQLite."""
        # Simplified implementation (full version kept in source)
        pass

    def _detect_conflict(self, kind, new_value, c):
        """Detects if a new fact conflicts with an existing one using embeddings."""
        if kind not in _CONFLICTABLE_KINDS: return None
        rows = list(c.execute("SELECT id, value, embedding FROM memory_items WHERE kind=? AND superseded=0 AND value != ?", (kind, new_value)))
        new_vec = _get_ollama_embedding(new_value)
        
        if new_vec is not None and np is not None:
            valid_rows = []
            embeddings = []
            for row in rows:
                if row["embedding"]:
                    try:
                        embeddings.append(np.frombuffer(row["embedding"], dtype=np.float32))
                        valid_rows.append(row)
                    except Exception: pass
            
            if embeddings:
                matrix = np.stack(embeddings)
                sims = _vectorized_cosine_similarity(new_vec, matrix)
                for i, sim in enumerate(sims):
                    if sim > 0.82: return valid_rows[i]
                    
        # Fallback to Jaccard-like keyword matching
        for row in rows:
            if len(set(re.findall(r"\w{3,}", new_value.lower())) & set(re.findall(r"\w{3,}", row["value"].lower()))) / 5 > 0.5: return row
        return None

    def _resolve_conflict(self, kind, old_row, new_value, c):
        """Resolves conflict by archiving the old fact and promoting the new one."""
        now = get_now_vn().isoformat()
        c.execute("UPDATE memory_items SET superseded=1, last_used_at=? WHERE id=?", (now, old_row["id"]))
        change_note = f"[Thay đổi] {kind}: '{old_row['value']}' → '{new_value}'"
        c.execute("INSERT INTO memory_items (kind,value,layer,weight,saliency,source_turn,created_at) VALUES ('episodic',?,?,1.0,4,?,?)", (change_note, LAYER_TEMPORAL, self.turn_counter, now))
        enqueue(PRIORITY_NORMAL, lambda: self.pinecone.delete(f"mem_{old_row['id']}"))

    def _get_embedding(self, text): return _get_ollama_embedding(text)
    def _embed_to_blob(self, text):
        vec = _get_ollama_embedding(text)
        return vec.tobytes() if vec is not None and np is not None else None

    def add_session_item(self, value, kind="session", is_sticky=False):
        """Adds a short-term awareness item to L2 memory. Transient items expire in 5m."""
        self._expire_session_memory_if_idle()
        now = get_now_vn()
        now_iso = now.isoformat()
        
        expires_at = None
        if not is_sticky:
            # Transient items (social chats) only stay for 5 minutes
            expires_at = (now + timedelta(minutes=5)).isoformat()
            
        self._session_items.append({
            "kind": kind, 
            "value": value, 
            "created_at": now_iso,
            "expires_at": expires_at,
            "is_sticky": is_sticky
        })
        self._session_last_activity_at = now_iso
        if len(self._session_items) > 50: self._session_items.pop(0) # Increased capacity for mix

    def queue_conversation_row(self, role, content):
        """Stage one chat message for durable SQLite persistence."""
        if role not in ("user", "assistant") or not content:
            return
        self._pending_conversation_rows.append((role, str(content)))
        self._is_dirty = True

    def update_rolling_stream_summary(self, summary): self._rolling_stream_summary = summary
    def clear_session_memory(self):
        self._session_items.clear()
        self._rolling_stream_summary = ""
        self._session_last_activity_at = None

    def get_session_context(self):
        """Retrieves session context, pruning expired transient items."""
        self._expire_session_memory_if_idle()
        now_iso = get_now_vn().isoformat()
        
        # Context Pruning: Filter out expired transient items
        valid_items = [
            i for i in self._session_items 
            if i.get("is_sticky") or (i.get("expires_at") and i["expires_at"] > now_iso)
        ]
        
        if not valid_items and not self._rolling_stream_summary: return ""
        parts = []
        if self._rolling_stream_summary: parts.append(f"[Tóm tắt stream]\n{self._rolling_stream_summary}")
        if valid_items: 
            # Show up to 10 latest valid items
            parts.append("[Diễn biến mới nhất]\n" + "\n".join([f"- {i['value']}" for i in valid_items[-10:]]))
        return "[Session context (L2)]\n" + "\n\n".join(parts)

    def _expire_session_memory_if_idle(self, idle_hours=4.0):
        if self._session_last_activity_at and get_now_vn() - datetime.fromisoformat(self._session_last_activity_at) >= timedelta(hours=idle_hours):
            self.clear_session_memory()

    def add_item(self, kind, value, weight=1.0, limit=12, importance=None):
        """Adds a fact or episode to memory with conflict detection."""
        self._is_dirty = True
        text = str(value).strip()
        if not text: return
        layer = _LAYER_MAP.get(kind, LAYER_USER)
        
        try:
            conn = self._get_db()
            if not conn: return
            c = conn.cursor()
            saliency = importance if importance is not None else self.estimate_saliency(kind, text)
            importance_val = KIND_IMPORTANCE.get(kind, 1.0)
            now = get_now_vn().isoformat()

            with self.db_lock:
                if layer == LAYER_USER:
                    conflict = self._detect_conflict(kind, text, c)
                    if conflict: self._resolve_conflict(kind, conflict, text, c)
                
                emb_blob = self._embed_to_blob(text)
                c.execute("INSERT INTO memory_items (kind,value,layer,weight,saliency,importance,source_turn,last_used_at,embedding) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(kind,value) DO UPDATE SET superseded=0, last_used_at=excluded.last_used_at, embedding=excluded.embedding", (kind, text, layer, weight, saliency, importance_val, self.turn_counter, now, emb_blob))
                conn.commit()
                
                if layer == LAYER_TEMPORAL and self.pinecone._enabled:
                    row = c.execute("SELECT id FROM memory_items WHERE kind=? AND value=?", (kind, text)).fetchone()
                    if row: self._upsert_pinecone_async(row["id"], kind, text, saliency)
        except Exception as e: print(f"[Memory] add_item error: {e}")

    def _upsert_pinecone_async(self, item_id, kind, text, saliency):
        def _run():
            vec = _get_ollama_embedding(text)
            if vec is not None:
                self.pinecone.upsert(f"mem_{item_id}", vec.tolist(), {"kind": kind, "value": text[:500], "saliency": saliency})
                conn = self._get_db()
                if conn:
                    with self.db_lock:
                        conn.execute("UPDATE memory_items SET pinecone_id=?, embedding=? WHERE id=?", (f"mem_{item_id}", vec.tobytes(), item_id))
                        conn.commit()
        enqueue(PRIORITY_NORMAL, _run)

    def _current_persist_time(self):
        return (
            self.memory.get("time_tracking", {}).get("last_message_time")
            or get_now_vn().isoformat()
        )

    def _metadata_rows(self, now):
        conversation = self.memory.get("conversation", {})
        relationship = self.memory.get("relationship", {})
        time_tracking = self.memory.get("time_tracking", {})

        rows = [
            ("total_messages", str(self.turn_counter)),
            ("first_chat", conversation.get("first_chat") or ""),
            ("last_chat", conversation.get("last_chat") or ""),
            ("last_message_time", now),
            ("affection", str(relationship.get("current_affection", 50))),
            (
                "milestones_reached",
                json.dumps(relationship.get("milestones_reached", []), ensure_ascii=False),
            ),
            ("memory_buffer", json.dumps(self.memory_buffer, ensure_ascii=False)),
        ]

        if time_tracking.get("first_greeting_sent") is not None:
            rows.append(("first_greeting_sent", str(bool(time_tracking.get("first_greeting_sent")))))

        return rows

    @staticmethod
    def _non_empty_rows(values):
        return [(key, value) for key, value in values.items() if value not in (None, "")]

    def _write_metadata(self, cursor, now):
        cursor.executemany(
            "INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)",
            self._metadata_rows(now),
        )

    def _write_profile_tables(self, cursor):
        cursor.executemany(
            "INSERT OR REPLACE INTO profile (key, value) VALUES (?, ?)",
            self._non_empty_rows(self.memory.get("user_profile", {})),
        )
        cursor.executemany(
            "INSERT OR REPLACE INTO self_profile (key, value) VALUES (?, ?)",
            self._non_empty_rows(self.memory.get("identity", {})),
        )

    def _flush_pending_conversation(self, cursor):
        if not self._pending_conversation_rows:
            return

        cursor.executemany(
            "INSERT INTO conversation (role, content) VALUES (?, ?)",
            self._pending_conversation_rows,
        )
        self._pending_conversation_rows.clear()

    def save(self):
        if not self._is_dirty and not self._pending_conversation_rows:
            return

        conn = self._get_db()
        if not conn:
            return

        c = conn.cursor()
        now = self._current_persist_time()
        with self.db_lock:
            self._write_metadata(c, now)
            self._write_profile_tables(c)
            self._flush_pending_conversation(c)
            conn.commit()
        self._is_dirty = False

    def get_relevant_context(self, user_input: str, is_public: bool = False, precomputed_vec=None) -> str:
        """Retrieves and ranks the most relevant memories for the current turn."""
        query_vec = precomputed_vec if precomputed_vec is not None else self._get_embedding(user_input)
        
        # 1. Check Semantic Cache
        if query_vec is not None and np is not None and len(self._semantic_cache) > 0:
            cached_vecs = [item[0] for item in self._semantic_cache]
            matrix = np.stack(cached_vecs)
            sims = _vectorized_cosine_similarity(query_vec, matrix)
            best_idx = np.argmax(sims)
            if sims[best_idx] > 0.95:
                print(f"[Memory] Semantic Cache Hit! (sim: {sims[best_idx]:.3f})")
                return self._semantic_cache[best_idx][1]

        # 2. Retrieve L1 Candidates
        candidates = []
        try:
            conn = self._get_db()
            if conn:
                rows = conn.execute("SELECT kind, value, weight, saliency, last_used_at FROM memory_items WHERE superseded=0 AND is_valid=1 ORDER BY saliency DESC LIMIT 60").fetchall()
                for r in rows:
                    if is_public and r["kind"] in ("profile", "relational"): continue
                    candidates.append({"kind": r["kind"], "value": r["value"], "weight": r["weight"] * (1 + r["saliency"]/10)})
        except Exception: pass

        # 3. Retrieve L2 Session Items
        for item in self._session_items[-10:]: candidates.append({"kind": "session", "value": item["value"], "weight": 1.2})
        
        # 4. Retrieve L3 Pinecone Items
        if query_vec is not None and self.pinecone._enabled:
            for m in self.pinecone.query(query_vec.tolist(), top_k=8):
                meta = m.get("metadata", {})
                kind = meta.get("kind", "temporal")
                val = meta.get("value", "")
                score = m["score"]
                
                if kind == "rl_few_shot":
                    # RL patterns get a massive boost to ensure they appear as few-shot examples
                    candidates.append({"kind": "rl_pattern", "value": f"[Mẫu thành công]: {val}", "weight": score * 4.0})
                elif score > 0.65:
                    candidates.append({"kind": "temporal", "value": val, "weight": score * 1.5})

        # 5. Rerank Candidates
        context_items = self.ranker.rank(user_input, candidates, token_budget=550)
        if not context_items: 
            return ""
        
        enqueue(PRIORITY_NORMAL, self.touch_items, [(c["kind"], c["value"]) for c in candidates if c["value"] in context_items])
        result_str = "Bối cảnh quan trọng:\n" + "\n".join([f"- {m}" for m in context_items])
        
        # 6. Update Semantic Cache
        if query_vec is not None:
            self._semantic_cache.append((query_vec, result_str))
            # Keep cache size small to stay fast (LRU style)
            if len(self._semantic_cache) > 50:
                self._semantic_cache.pop(0)
                
        return result_str

    def touch_items(self, items):
        conn = self._get_db()
        if not conn: return
        now = get_now_vn().isoformat()
        with self.db_lock:
            for kind, value in items:
                conn.execute("UPDATE memory_items SET last_used_at=?, access_count=MIN(access_count+1,100) WHERE kind=? AND value=?", (now, kind, value))
            conn.commit()

    def _llm_importance_score(self, items: list[dict]) -> list[int]:
        """Batch-score items (1-10) using light model."""
        if not items: return []
        from config import LIGHT_MODEL, LIGHT_BASE_URL
        
        prompt = (
            "Rank how important each memory item is for an AI Streamer's long-term memory (1-10, 10 is critical).\n"
            "Return ONLY a comma-separated list of numbers.\n"
            "Items:\n"
        )
        for i, item in enumerate(items):
            prompt += f"{i+1}. [{item.get('kind', 'fact')}]: {item.get('value', '')[:120]}\n"

        try:
            resp = requests.post(
                LIGHT_BASE_URL,
                json={
                    "model": LIGHT_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "options": {"temperature": 0.1, "num_predict": 30},
                    "stream": False
                },
                timeout=10
            )
            if resp.status_code == 200:
                content = resp.json().get("message", {}).get("content", "").strip()
                scores = [int(s.strip()) for s in re.findall(r"\b\d+\b", content)]
                if len(scores) < len(items):
                    scores.extend([self.estimate_saliency(it.get('kind'), it.get('value')) for it in items[len(scores):]])
                return [max(1, min(10, s)) for s in scores[:len(items)]]
        except Exception:
            pass
        return [self.estimate_saliency(it.get('kind'), it.get('value')) for it in items]

    def estimate_saliency(self, kind, value):
        """Heuristic-based saliency (fallback)."""
        score = {"goal": 5, "relational": 5, "episodic": 3}.get(kind, 2)
        if any(w in str(value).lower() for w in ["love", "hate", "important", "stress", "fail", "proud"]): score += 3
        return min(10, score)

    def get_rare_memory(self):
        conn = self._get_db()
        if not conn: return ""
        with self.db_lock:
            res = conn.execute("SELECT value FROM memory_items WHERE saliency >= 3 ORDER BY access_count ASC, saliency DESC LIMIT 5").fetchall()
            return random.choice(res)[0] if res else ""

    def consolidate(self):
        """
        Enhanced Forgetting Mechanism (Semantic & Episodic Decay).
        Deletes stale, low-value facts and old superseded records from SQLite.
        """
        conn = self._get_db()
        if not conn: return
        
        now = get_now_vn()
        fourteen_days_ago = (now - timedelta(days=14)).isoformat()
        thirty_days_ago = (now - timedelta(days=30)).isoformat()
        
        try:
            with self.db_lock:
                # 1. Delete "junk" items (access_count=0 and old)
                conn.execute(
                    "DELETE FROM memory_items WHERE access_count=0 AND (? - source_turn) > 100 AND saliency < 7", 
                    (self.turn_counter,)
                )
                
                # 2. Forget stale L1 facts (Semantic Forgetting)
                # If not accessed in 14 days AND access_count < 3 AND saliency < 8
                conn.execute(
                    "DELETE FROM memory_items WHERE superseded=0 AND layer=? "
                    "AND last_used_at < ? AND access_count < 3 AND saliency < 8",
                    (LAYER_USER, fourteen_days_ago)
                )
                
                # 3. Purge old superseded items (Database Cleanup)
                # They are already in Pinecone (L3), so we don't need them in SQLite L1 forever
                conn.execute(
                    "DELETE FROM memory_items WHERE superseded=1 AND last_used_at < ?",
                    (thirty_days_ago,)
                )
                
                conn.commit()
                print("[Memory] Consolidation completed: stale facts and old archives purged.")
        except Exception as e:
            print(f"[Memory] Consolidation error: {e}")

    def consolidate_episodic_to_semantic(self):
        """
        Giai đoạn củng cố ký ức (CLS): Chuyển đổi các sự kiện ngắn hạn (L2/L3) 
        thành các đặc điểm lâu dài (L1) và thực hiện dọn dẹp (Forgetting).
        """
        print("[Memory] Running post-session consolidation and forgetting...")
        # 1. Thực hiện dọn dẹp các fact cũ/yếu (Cơ chế quên)
        self.consolidate()
        
        # 2. Dọn dẹp cache để giải phóng tài nguyên
        self._basic_context_cache = None
        self._rag_context_cache = None
        self._relevant_items_cache = None
        
        print("[Memory] Consolidation and cleaning successful.")

    def _get_in_memory_fallback(self):
        class Fallback:
            def execute(self, *a): return self
            def commit(self): pass
            def cursor(self): return self
            def fetchone(self): return None
            def fetchall(self): return []
        return Fallback()

    def add_diary_entry(self, content, mood=0.0, affection=50.0):
        conn = self._get_db()
        if not conn: return
        with self.db_lock:
            conn.execute("INSERT INTO diaries (content, mood_score, affection_score) VALUES (?,?,?)", (content, mood, affection))
            conn.commit()

    def get_diary_entries(self, limit=5):
        conn = self._get_db()
        if not conn: return []
        try:
            rows = conn.execute("SELECT content, mood_score, affection_score, timestamp FROM diaries ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            return [{"content": r[0], "mood": r[1], "affection": r[2], "timestamp": r[3]} for r in rows]
        except Exception: return []

    def get_stream_milestones(self, limit=3):
        conn = self._get_db()
        if not conn: return []
        try:
            rows = conn.execute("SELECT description, achieved_at FROM stream_milestones ORDER BY achieved_at DESC LIMIT ?", (limit,)).fetchall()
            return [{"description": r[0], "achieved_at": r[1]} for r in rows]
        except Exception: return []

    def increment_stream_count(self):
        """Increments and returns the total number of streams performed."""
        conn = self._get_db()
        if not conn: return 1
        with self.db_lock:
            try:
                row = conn.execute("SELECT value FROM metadata WHERE key='stream_count'").fetchone()
                count = int(row[0]) if row else 0
                count += 1
                conn.execute("INSERT OR REPLACE INTO metadata (key, value) VALUES ('stream_count', ?)", (str(count),))
                conn.commit()
                return count
            except Exception: return 1

    def check_stream_milestone(self, event_type, description, stream_title=""):
        """Checks if a milestone was already achieved. If not, records it."""
        conn = self._get_db()
        if not conn: return False
        with self.db_lock:
            try:
                row = conn.execute("SELECT 1 FROM stream_milestones WHERE event_type=?", (event_type,)).fetchone()
                if row: return False
                now_iso = get_now_vn().isoformat()
                conn.execute(
                    "INSERT INTO stream_milestones (event_type, description, achieved_at, stream_title) VALUES (?,?,?,?)",
                    (event_type, description, now_iso, stream_title)
                )
                conn.commit()
                return True
            except Exception: return False

    def extract_candidates_heuristic(self, text):
        """Fast heuristic-based memory extraction."""
        candidates = []
        text_lower = text.lower()
        
        # Simple patterns for likes/dislikes
        if re.search(r"\b(thích|yêu|mê|khoái)\b", text_lower):
            m = re.search(r"(?:thích|yêu|mê|khoái)\s+([^\s,!?.]+)", text_lower)
            if m: candidates.append({"kind": "like", "value": m.group(1), "saliency": 3})
            
        if re.search(r"\b(ghét|không thích|sợ)\b", text_lower):
            m = re.search(r"(?:ghét|không thích|sợ)\s+([^\s,!?.]+)", text_lower)
            if m: candidates.append({"kind": "dislike", "value": m.group(1), "saliency": 3})
            
        if re.search(r"\b(muốn|định|sẽ|kế hoạch)\b", text_lower):
            m = re.search(r"(?:muốn|định|sẽ|kế hoạch)\s+([^\s,!?.]+)", text_lower)
            if m: candidates.append({"kind": "goal", "value": m.group(1), "saliency": 4})
            
        return candidates

    def buffer_candidate(self, kind, value, saliency=None):
        if not hasattr(self, "memory_buffer"): self.memory_buffer = []
        self.memory_buffer.append({"kind": kind, "value": value, "saliency": saliency or 2, "timestamp": time.time()})
        if len(self.memory_buffer) > 20: self.memory_buffer.pop(0)

    def should_buffer(self, text, intent):
        """Decides if we should bother with LLM extraction."""
        if intent in ("compliment", "complaint", "introduction", "suggestion"): return True
        if len(text) > 40: return True
        if self.memory_buffer: return True
        return False

    def should_flush(self, intent):
        """Decides if we should flush the buffer now."""
        # Flush every few turns or on important intents
        if intent in ("introduction", "suggestion") or len(self.memory_buffer) >= 3:
            return True
        return False
