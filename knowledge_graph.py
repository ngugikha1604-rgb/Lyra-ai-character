"""
knowledge_graph.py — Cognee-style knowledge graph cho Lyra.

Thiết kế:
  - Nodes: thực thể (game, nhân vật, khái niệm, người nổi tiếng...)
  - Edges: quan hệ giữa các nodes (phát_triển_bởi, ra_mắt_năm, thuộc_thể_loại...)
  - Pipeline: chat → detect teaching intent → extract triplets (background LLM)
              → pending_kg table → Lyra hỏi xác nhận → confirmed → kg_nodes/kg_edges
  - Retrieve: semantic search trên node labels + graph traversal 1-hop
  - Inject vào prompt khi: câu hỏi liên quan chủ đề node đã có,
    hoặc Lyra chủ động nhắc lại khi context phù hợp.

SQLite tables:
  kg_nodes  (id, label, node_type, description, source_viewer, confidence,
             created_at, updated_at, access_count, embedding, pinecone_id)
  kg_edges  (id, from_id, to_id, relation, weight, created_at)
  kg_pending (id, raw_text, triplets_json, source_viewer, created_at, status)
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from typing import TYPE_CHECKING

from memory_utils import (
    DB_PATH, DB_LOCK,
    _get_ollama_embedding, _cosine_similarity,
    get_now_vn,
)
from background_worker import enqueue, PRIORITY_NORMAL, PRIORITY_HIGH

try:
    import numpy as np
except ImportError:
    np = None


# ─── Từ khoá nhận biết "viewer đang dạy kiến thức" ─────────────────────────── #
_TEACH_PATTERNS = re.compile(
    r"(?:"
    r"bi[eế]t kh[oô]ng|b[aả]o cho|bi[eế]t \w+ ch[ưa]|mình bi[eế]t|"
    r"th[uự]c ra|th[aự]c t[eế]|đúng là|[lL]à do|[lL]àm b[ởơ]i|"
    r"ra m[aắ]t n[aă]m|ph[aá]t tri[eể]n b[ởơ]i|thu[oộ]c th[eể] lo[aạ]i|"
    r"made by|developed by|released in|actually|fun fact|did you know"
    r")",
    re.I | re.UNICODE,
)


class KnowledgeGraph:
    """
    Singleton. Khởi tạo một lần, inject _call_light_model từ core.py.

    Usage:
        kg = KnowledgeGraph()
        kg.set_llm(lyra_ai._call_light_model)

        # Mỗi turn:
        pending = kg.maybe_extract(message, sender_name, source_type)
        if pending:
            # Lyra nên hỏi xác nhận trong reply
            pass

        # Khi user xác nhận:
        kg.confirm_pending(pending_id)

        # Lấy context:
        ctx = kg.get_context(user_input, top_k=5)
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._llm = None
        self._init_schema()

    def set_llm(self, call_light_model) -> None:
        self._llm = call_light_model

    # ------------------------------------------------------------------ #
    # Schema                                                               #
    # ------------------------------------------------------------------ #

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=60.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_schema(self) -> None:
        conn = self._get_conn()
        with DB_LOCK:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS kg_nodes (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    label         TEXT NOT NULL,
                    node_type     TEXT DEFAULT 'concept',
                    description   TEXT DEFAULT '',
                    source_viewer TEXT DEFAULT '',
                    confidence    REAL DEFAULT 1.0,
                    access_count  INTEGER DEFAULT 0,
                    embedding     BLOB,
                    pinecone_id   TEXT,
                    created_at    TEXT DEFAULT (datetime('now')),
                    updated_at    TEXT DEFAULT (datetime('now')),
                    UNIQUE(label)
                );

                CREATE TABLE IF NOT EXISTS kg_edges (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    from_id    INTEGER NOT NULL REFERENCES kg_nodes(id),
                    to_id      INTEGER NOT NULL REFERENCES kg_nodes(id),
                    relation   TEXT NOT NULL,
                    weight     REAL DEFAULT 1.0,
                    created_at TEXT DEFAULT (datetime('now')),
                    UNIQUE(from_id, to_id, relation)
                );

                CREATE TABLE IF NOT EXISTS kg_pending (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    raw_text      TEXT NOT NULL,
                    triplets_json TEXT NOT NULL DEFAULT '[]',
                    source_viewer TEXT DEFAULT '',
                    created_at    TEXT DEFAULT (datetime('now')),
                    status        TEXT DEFAULT 'waiting'
                );

                CREATE INDEX IF NOT EXISTS idx_kg_edges_from ON kg_edges(from_id);
                CREATE INDEX IF NOT EXISTS idx_kg_edges_to   ON kg_edges(to_id);
            """)
            conn.commit()
        conn.close()
        print("[KG] Schema initialized.")

    # ------------------------------------------------------------------ #
    # Extract pipeline                                                     #
    # ------------------------------------------------------------------ #

    def maybe_extract(
        self, message: str, sender_name: str, source_type: str
    ) -> dict | None:
        """
        Kiểm tra xem message có chứa kiến thức mới không.
        Nếu có → tạo pending record → background LLM extract triplets.
        Trả về dict pending hoặc None.
        """
        if len(message.strip()) < 10:
            return None
        if not _TEACH_PATTERNS.search(message):
            return None

        pending_id = self._create_pending(message, sender_name)
        if pending_id is None:
            return None

        enqueue(
            PRIORITY_HIGH,
            self._extract_and_update_pending,
            pending_id, message, sender_name,
        )
        return {"pending_id": pending_id, "raw_text": message, "sender": sender_name}

    def _create_pending(self, raw_text: str, sender_name: str) -> int | None:
        try:
            conn = self._get_conn()
            with DB_LOCK:
                cur = conn.execute(
                    "INSERT INTO kg_pending (raw_text, triplets_json, source_viewer, status) "
                    "VALUES (?, '[]', ?, 'extracting')",
                    (raw_text, sender_name),
                )
                pid = cur.lastrowid
                conn.commit()
            conn.close()
            return pid
        except Exception as e:
            print(f"[KG] _create_pending error: {e}")
            return None

    def _extract_and_update_pending(
        self, pending_id: int, raw_text: str, sender_name: str
    ) -> None:
        """Background: gọi LLM extract triplets, cập nhật pending record."""
        if not self._llm:
            return

        prompt = [
            {
                "role": "system",
                "content": (
                    "Trích xuất các facts từ câu sau dưới dạng JSON array.\n"
                    "Mỗi item: {\"subject\": \"...\", \"relation\": \"...\", \"object\": \"...\"}\n"
                    "Relation dùng dạng snake_case tiếng Việt không dấu "
                    "(vd: phat_trien_boi, ra_mat_nam, thuoc_the_loai, co_tinh_nang).\n"
                    "Chỉ trả về JSON array thuần, không giải thích, không markdown."
                ),
            },
            {"role": "user", "content": f'Câu: "{raw_text}"'},
        ]

        try:
            raw_resp = self._llm(
                prompt, temperature=0.1, max_tokens=150, provider="gemini"
            ) or ""
            raw_resp = re.sub(r"```json|```", "", raw_resp).strip()
            triplets: list = json.loads(raw_resp) if raw_resp.startswith("[") else []

            valid = [
                t for t in triplets
                if isinstance(t, dict)
                and t.get("subject") and t.get("relation") and t.get("object")
                and len(str(t["subject"])) >= 2
            ]

            conn = self._get_conn()
            with DB_LOCK:
                conn.execute(
                    "UPDATE kg_pending SET triplets_json=?, status='waiting' WHERE id=?",
                    (json.dumps(valid, ensure_ascii=False), pending_id),
                )
                conn.commit()
            conn.close()
            print(
                f"[KG] Extracted {len(valid)} triplet(s) for pending #{pending_id}: {valid}"
            )

        except Exception as e:
            print(f"[KG] extract error for pending #{pending_id}: {e}")
            try:
                conn2 = self._get_conn()
                with DB_LOCK:
                    conn2.execute(
                        "UPDATE kg_pending SET status='failed' WHERE id=?",
                        (pending_id,),
                    )
                    conn2.commit()
                conn2.close()
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    # Confirmation                                                         #
    # ------------------------------------------------------------------ #

    def confirm_pending(self, pending_id: int) -> bool:
        """
        Lyra xác nhận kiến thức → lưu vào kg_nodes + kg_edges.
        Trả về True nếu thành công.
        """
        try:
            conn = self._get_conn()
            row = conn.execute(
                "SELECT * FROM kg_pending WHERE id=?", (pending_id,)
            ).fetchone()
            conn.close()

            if not row:
                return False

            # Nếu LLM chưa xong — đợi tối đa 8s
            if row["status"] == "extracting":
                for _ in range(8):
                    time.sleep(1.0)
                    conn2 = self._get_conn()
                    row = conn2.execute(
                        "SELECT * FROM kg_pending WHERE id=?", (pending_id,)
                    ).fetchone()
                    conn2.close()
                    if row["status"] != "extracting":
                        break

            triplets: list = json.loads(row["triplets_json"] or "[]")
            if not triplets:
                return False

            source = row["source_viewer"]
            now    = get_now_vn().isoformat()

            conn3 = self._get_conn()
            with DB_LOCK:
                for t in triplets:
                    subj = str(t["subject"]).strip()
                    rel  = str(t["relation"]).strip()
                    obj  = str(t["object"]).strip()

                    from_id = self._upsert_node(conn3, subj, source, now)
                    to_id   = self._upsert_node(conn3, obj,  source, now)

                    if from_id and to_id:
                        conn3.execute(
                            "INSERT OR IGNORE INTO kg_edges "
                            "(from_id, to_id, relation, created_at) VALUES (?,?,?,?)",
                            (from_id, to_id, rel, now),
                        )

                conn3.execute(
                    "UPDATE kg_pending SET status='confirmed' WHERE id=?",
                    (pending_id,),
                )
                conn3.commit()
            conn3.close()

            enqueue(PRIORITY_NORMAL, self._embed_recent_nodes)
            print(
                f"[KG] Confirmed pending #{pending_id}: {len(triplets)} triplet(s) saved."
            )
            return True

        except Exception as e:
            print(f"[KG] confirm_pending error: {e}")
            return False

    def deny_pending(self, pending_id: int) -> None:
        """Viewer hoặc owner từ chối → đánh dấu denied."""
        try:
            conn = self._get_conn()
            with DB_LOCK:
                conn.execute(
                    "UPDATE kg_pending SET status='denied' WHERE id=?", (pending_id,)
                )
                conn.commit()
            conn.close()
        except Exception as e:
            print(f"[KG] deny_pending error: {e}")

    def _upsert_node(
        self, conn: sqlite3.Connection, label: str, source: str, now: str
    ) -> int | None:
        """Tạo hoặc cập nhật node. Trả về node id."""
        try:
            row = conn.execute(
                "SELECT id FROM kg_nodes WHERE label=?", (label,)
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE kg_nodes SET updated_at=?, access_count=access_count+1 WHERE id=?",
                    (now, row["id"]),
                )
                return row["id"]
            cur = conn.execute(
                "INSERT INTO kg_nodes (label, source_viewer, created_at, updated_at) "
                "VALUES (?,?,?,?)",
                (label, source, now, now),
            )
            return cur.lastrowid
        except Exception as e:
            print(f"[KG] _upsert_node error for '{label}': {e}")
            return None

    # ------------------------------------------------------------------ #
    # Embedding                                                            #
    # ------------------------------------------------------------------ #

    def _embed_recent_nodes(self) -> None:
        """Embed các node chưa có embedding — chạy background."""
        if np is None:
            return
        try:
            conn = self._get_conn()
            rows = conn.execute(
                "SELECT id, label FROM kg_nodes WHERE embedding IS NULL LIMIT 20"
            ).fetchall()
            for row in rows:
                vec = _get_ollama_embedding(row["label"])
                if vec is not None:
                    with DB_LOCK:
                        conn.execute(
                            "UPDATE kg_nodes SET embedding=? WHERE id=?",
                            (vec.tobytes(), row["id"]),
                        )
            with DB_LOCK:
                conn.commit()
            conn.close()
        except Exception as e:
            print(f"[KG] _embed_recent_nodes error: {e}")

    # ------------------------------------------------------------------ #
    # Retrieval                                                            #
    # ------------------------------------------------------------------ #

    def get_context(self, query: str, top_k: int = 4) -> str:
        """
        Tìm kiếm knowledge graph liên quan đến query.
        1. Semantic search trên node labels (embedding cosine)
        2. 1-hop traversal: lấy edges của matching nodes
        Trả về context string để inject vào prompt.
        """
        try:
            conn = self._get_conn()
            nodes = conn.execute(
                "SELECT id, label, embedding FROM kg_nodes "
                "WHERE embedding IS NOT NULL ORDER BY access_count DESC LIMIT 200"
            ).fetchall()
            conn.close()

            if not nodes:
                return ""

            matched_ids: list[int] = []
            query_vec = _get_ollama_embedding(query)

            if query_vec is not None and np is not None:
                scores: list[tuple[float, int]] = []
                for node in nodes:
                    try:
                        vec = np.frombuffer(node["embedding"], dtype=np.float32)
                        sim = float(_cosine_similarity(query_vec, vec))
                        scores.append((sim, node["id"]))
                    except Exception:
                        pass
                scores.sort(reverse=True)
                matched_ids = [s[1] for s in scores[:top_k] if s[0] > 0.65]
            else:
                # Keyword fallback khi không có embedding
                q_lower = query.lower()
                for node in nodes:
                    if node["label"].lower() in q_lower:
                        matched_ids.append(node["id"])
                        if len(matched_ids) >= top_k:
                            break

            if not matched_ids:
                return ""

            return self._build_context_from_nodes(matched_ids)

        except Exception as e:
            print(f"[KG] get_context error: {e}")
            return ""

    def _build_context_from_nodes(self, node_ids: list[int]) -> str:
        """Lấy edges của các node và format thành context string."""
        try:
            conn = self._get_conn()
            lines: list[str] = []
            seen_edges: set[tuple] = set()

            for nid in node_ids:
                edges = conn.execute(
                    """
                    SELECT n1.label AS from_label, e.relation, n2.label AS to_label
                    FROM kg_edges e
                    JOIN kg_nodes n1 ON e.from_id = n1.id
                    JOIN kg_nodes n2 ON e.to_id   = n2.id
                    WHERE e.from_id=? OR e.to_id=?
                    LIMIT 6
                    """,
                    (nid, nid),
                ).fetchall()

                for e in edges:
                    key = (e["from_label"], e["relation"], e["to_label"])
                    if key not in seen_edges:
                        seen_edges.add(key)
                        lines.append(
                            f"{e['from_label']} — "
                            f"{e['relation'].replace('_', ' ')} — "
                            f"{e['to_label']}"
                        )

                with DB_LOCK:
                    conn.execute(
                        "UPDATE kg_nodes SET access_count=access_count+1 WHERE id=?",
                        (nid,),
                    )

            with DB_LOCK:
                conn.commit()
            conn.close()

            if not lines:
                return ""

            return "[KIẾN THỨC LYRA ĐÃ HỌC]\n" + "\n".join(
                f"• {line}" for line in lines[:8]
            )

        except Exception as e:
            print(f"[KG] _build_context_from_nodes error: {e}")
            return ""

    # ------------------------------------------------------------------ #
    # State helpers                                                        #
    # ------------------------------------------------------------------ #

    def get_pending(self, pending_id: int) -> dict | None:
        try:
            conn = self._get_conn()
            row = conn.execute(
                "SELECT * FROM kg_pending WHERE id=?", (pending_id,)
            ).fetchone()
            conn.close()
            return dict(row) if row else None
        except Exception:
            return None

    def has_waiting_pending(self) -> bool:
        try:
            conn = self._get_conn()
            row = conn.execute(
                "SELECT 1 FROM kg_pending WHERE status='waiting' LIMIT 1"
            ).fetchone()
            conn.close()
            return row is not None
        except Exception:
            return False

    def get_oldest_waiting(self) -> dict | None:
        try:
            conn = self._get_conn()
            row = conn.execute(
                "SELECT * FROM kg_pending WHERE status='waiting' ORDER BY id ASC LIMIT 1"
            ).fetchone()
            conn.close()
            return dict(row) if row else None
        except Exception:
            return None

    def get_stats(self) -> dict:
        try:
            conn = self._get_conn()
            n_nodes   = conn.execute("SELECT COUNT(*) FROM kg_nodes").fetchone()[0]
            n_edges   = conn.execute("SELECT COUNT(*) FROM kg_edges").fetchone()[0]
            n_pending = conn.execute(
                "SELECT COUNT(*) FROM kg_pending WHERE status='waiting'"
            ).fetchone()[0]
            conn.close()
            return {"nodes": n_nodes, "edges": n_edges, "pending": n_pending}
        except Exception:
            return {"nodes": 0, "edges": 0, "pending": 0}


# Singleton — import và dùng trực tiếp
knowledge_graph = KnowledgeGraph()
