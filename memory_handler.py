import re
import json
import time
from datetime import datetime
from prompts import MEMORY_EXTRACT_SYSTEM, SUMMARIZE_PROMPT, MEMORY_COMPRESSION_PROMPT
from config import SUMMARY_TRIGGER, MAX_SUMMARIES

class MemoryHandlerMixin:
    """
    Mixin for Lyra's memory processing logic within the core engine.
    Handles extraction and summarization.
    """

    def extract_memory(self, text, intent, source_type="owner"):
        """Extracts new memories from conversation and saves to L1 and L3."""
        now_ts = datetime.now().isoformat()
        convo = self.memory.memory.setdefault("conversation", {})
        if not convo.get("first_chat"):
            convo["first_chat"] = now_ts
        convo["last_chat"] = now_ts

        name_patterns = [
            r"(?:my name is|i'm called|call me|my name's) ([a-zA-Z]+)",
            r"(?:you can call me) ([a-zA-Z]+)",
            r"(?:tên mình là|tên tao là|gọi mình là|tên tôi là) ([^\s,!?.]+)",
        ]
        skip_words = {"lyra", "coding", "python", "javascript", "game", "an", "ai", "the", "not", "just", "also", "really"}

        for pattern in name_patterns:
            m = re.search(pattern, text, re.IGNORECASE)
            if m:
                name = m.group(1).strip()
                if name.lower() not in skip_words and len(name) > 1:
                    self.memory.memory["user_profile"]["name"] = name
                    print(f"✓ Stored name: {name}")
                    break

        for candidate in self.memory.extract_candidates_heuristic(text):
            self.memory.buffer_candidate(candidate["kind"], candidate["value"], candidate.get("saliency"))

        if not self.memory.should_buffer(text, intent) or not self.memory.should_flush(intent):
            return

        recent = self.messages[-4:] if len(self.messages) >= 4 else self.messages
        convo_snippet = "\n".join([f"{'User' if m['role'] == 'user' else 'Lyra'}: {m['content']}" for m in recent if isinstance(m, dict)])
        convo_snippet += f"\nUser: {text}"

        known = {
            "name": self.memory.memory["user_profile"].get("name", ""),
            "location": self.memory.memory["user_profile"].get("location", ""),
            "occupation": self.memory.memory["user_profile"].get("occupation", ""),
            "likes": self.memory.memory["preferences"]["likes"][:5],
            "goals": self.memory.memory["facts"].get("goals", [])[:3],
        }

        extract_prompt = [
            {
                "role": "system",
                "content": f"{MEMORY_EXTRACT_SYSTEM}\n\nAlready known: {json.dumps(known)}\nBuffered candidates: {json.dumps(self.memory.memory_buffer[-8:], ensure_ascii=False)}",
            },
            {"role": "user", "content": f"Conversation:\n{convo_snippet}"},
        ]

        try:
            raw = self._call_light_model(extract_prompt, temperature=0.1, max_tokens=200, provider="background") or ""
            raw = re.sub(r"```json|```", "", raw).strip()
            if not raw or raw == "{}": return

            facts = json.loads(raw)
            profile = self.memory.memory["user_profile"]

            # Collect all items to batch-score importance
            items_to_add = []
            if source_type == "owner":
                for k in ["name", "location", "occupation"]:
                    if facts.get(k) and not profile.get(k): profile[k] = facts[k]
                if facts.get("age") and not profile.get("age_range"): profile["age_range"] = facts["age"]
                for item in facts.get("relational", []):
                    items_to_add.append({"kind": "relational", "value": item, "weight": 1.3})

            for kind, key in [("like", "likes"), ("dislike", "dislikes"), ("goal", "goals"), ("topic", "topics"), ("inside_joke", "inside_jokes")]:
                weight = 1.4 if kind == "goal" else 1.5 if kind == "inside_joke" else 1.0
                for item in facts.get(key, []):
                    items_to_add.append({"kind": kind, "value": item, "weight": weight})

            # Batch score importance
            if items_to_add:
                scores = self.memory._llm_importance_score(items_to_add)
                for i, item in enumerate(items_to_add):
                    saliency = scores[i] if i < len(scores) else None
                    self.memory.add_item(item["kind"], item["value"], weight=item["weight"], importance=saliency)

            if facts.get("mood_today"): self._user_mood_today = facts["mood_today"]
            self.memory.memory_buffer.clear()
            self.memory._is_dirty = True
        except Exception as e:
            print(f"[MemoryHandler] Extraction failed: {e}")
        self.memory.save()

    def summarize_history(self):
        """Summarizes history when threshold reached."""
        with self._msg_lock:
            if len(self.messages) < SUMMARY_TRIGGER or self.turn_counter % 2 != 0:
                return

            to_summarize = list(self.messages[:SUMMARY_TRIGGER])
            trim_index = SUMMARY_TRIGGER
        
        convo_text = "\n".join([f"{'User' if m['role'] == 'user' else 'Lyra'}: {m['content']}" for m in to_summarize if isinstance(m, dict)])

        if not convo_text.strip():
            with self._msg_lock:
                self.messages = self.messages[SUMMARY_TRIGGER:]
            return

        try:
            summary = self._call_light_model([
                {"role": "system", "content": SUMMARIZE_PROMPT},
                {"role": "user", "content": f"Summarize this conversation:\n\n{convo_text}"},
            ], temperature=0.4, max_tokens=120, provider="background")

            if summary := summary.strip():
                ts = self.current_time.strftime("%Y-%m-%d %H:%M")
                self.save_summary_to_db(summary, ts)
                self.memory.add_item("episodic", summary, weight=1.2, limit=8)
                self.memory.memory["conversation"]["chat_history_summary"].append({"timestamp": ts, "summary": summary, "is_mega": False})
                if len(self.memory.memory["conversation"]["chat_history_summary"]) > MAX_SUMMARIES + 1:
                    self.memory.memory["conversation"]["chat_history_summary"].pop(1)
                with self._msg_lock:
                    self.messages = self.messages[trim_index:]
                print(f"✓ History summarized: {ts}")
        except Exception as e:
            print(f"[MemoryHandler] Summarization failed: {e}")

    def save_summary_to_db(self, text, timestamp):
        """Standardizes summary storage with compression logic."""
        conn = self.memory._get_db()
        if not conn: return
        c = conn.cursor()

        try:
            with self.memory.db_lock:
                res = c.execute("SELECT COUNT(*) FROM summaries WHERE is_mega=0").fetchone()
                count = res[0] if res else 0
                if count >= self.memory.max_summaries:
                    old_summaries = c.execute("SELECT id, summary, timestamp FROM summaries WHERE is_mega=0 ORDER BY id ASC").fetchall()
                    old_mega = c.execute("SELECT summary FROM summaries WHERE is_mega=1 ORDER BY id DESC LIMIT 1").fetchone()
                else:
                    old_summaries, old_mega = [], None

            if count >= self.memory.max_summaries:
                parts = ([f"[Previous long-term memory]: {old_mega[0]}"] if old_mega else []) + [f"[{row[2]}] {row[1]}" for row in old_summaries]
                try:
                    mega_text = self._call_light_model([
                        {"role": "system", "content": MEMORY_COMPRESSION_PROMPT},
                        {"role": "user", "content": f"Compress these summaries:\n\n" + "\n".join(parts)},
                    ], temperature=0.3, max_tokens=200, provider="background")
                    if mega_text:
                        with self.memory.db_lock:
                            c.execute("DELETE FROM summaries WHERE is_mega=1")
                            c.execute("DELETE FROM summaries WHERE is_mega=0")
                            c.execute("INSERT INTO summaries (summary, timestamp, is_mega) VALUES (?,?,1)", (mega_text, datetime.now().strftime("%Y-%m-%d %H:%M")))
                            conn.commit()
                except Exception as e:
                    print(f"[MemoryHandler] Mega summary failed: {e}")

            with self.memory.db_lock:
                c.execute("INSERT INTO summaries (summary, timestamp, is_mega) VALUES (?,?,0)", (text, timestamp))
                conn.commit()
        finally:
            pass  # Không close connection — _get_db() trả về persistent conn được cache.
                  # Việc close sẽ kill connection cho toàn bộ MemorySystem cho đến khi reconnect.
                  # SQLite WAL mode tự handle concurrent access mà không cần close giữa chừng.
