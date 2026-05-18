import json
import re
import requests
from datetime import datetime

class MemoryConsolidator:
    """
    Complementary Learning System (CLS) - The 'sleep phase' of Lyra's brain.
    Processes episodic memories to detect patterns and update long-term semantic traits.
    """
    def __init__(self, light_model="qwen2.5:0.5b", base_url="http://localhost:11434/api/chat"):
        self.model = light_model
        self.url = base_url
        self._session = requests.Session()

    def distill_episodic_memories(self, episodes: list[str], current_facts: dict) -> list[dict]:
        """
        Analyzes a list of episodic events and extracts stable, long-term facts.
        """
        if not episodes:
            return []

        prompt = (
            "You are Lyra's subconscious memory consolidator. Based on today's events, "
            "extract stable findings that should be part of Lyra's permanent knowledge (L1 Memory).\n\n"
            "Today's events:\n" + "\n".join(f"- {e}" for e in episodes) + "\n\n"
            "Existing knowledge focus:\n"
            f"- Likes: {current_facts.get('likes', [])}\n"
            f"- Goals: {current_facts.get('goals', [])}\n\n"
            "Task: Identify 1-3 NEW or UPDATED facts/preferences worth remembering forever. "
            "Ignore trifles. Keep items extremely concise (3-8 words).\n"
            "Return JSON ONLY: {\"updates\": [{\"kind\": \"like|dislike|goal|topic|relational\", \"value\": \"...\"}]}"
        )

        try:
            resp = self._session.post(
                self.url,
                json={
                    "model": self.model,
                    "messages": [{"role": "user", "content": prompt}],
                    "options": {"temperature": 0.3, "num_predict": 250},
                    "stream": False
                },
                timeout=30
            )
            if resp.status_code == 200:
                raw = resp.json().get("message", {}).get("content", "").strip()
                # Use regex to find the most likely JSON block containing "updates"
                json_match = re.search(r"(\{[\s\S]*?\"updates\"[\s\S]*?\})", raw)
                if not json_match: # Fallback to greedy if non-greedy fails
                    json_match = re.search(r"(\{[\s\S]*\})", raw)
                
                if json_match:
                    try:
                        return json.loads(json_match.group(0)).get("updates", [])
                    except json.JSONDecodeError:
                        # Final attempt: try to find the largest valid JSON substring
                        pass 
                return []
        except Exception as e:
            print(f"[CLS] Consolidation error: {e}")
        
        return []

    def update_personality_indices(self, episodes: list[str], stream_summary: str) -> dict:
        """
        Analyzes the vibe and updates Lyra's behavioral indices.
        Returns a dict of suggested index adjustments.
        """
        if not episodes and not stream_summary:
            return {}

        prompt = (
            "Analyze the following stream events and summary. Determine if Lyra's internal "
            "vibe should shift. \n\n"
            f"Summary: {stream_summary}\n"
            f"Events:\n" + "\n".join(episodes[:10]) + "\n\n"
            "Return JSON with shifts (-2.0 to +2.0) for: mood_bias, affection_rate, attention_baseline.\n"
            "Example: {\"mood_bias\": 0.5, \"affection_rate\": -0.2}"
        )

        try:
            resp = self._session.post(
                self.url,
                json={
                    "model": self.model,
                    "messages": [{"role": "user", "content": prompt}],
                    "options": {"temperature": 0.1, "num_predict": 100},
                    "stream": False
                },
                timeout=15
            )
            if resp.status_code == 200:
                raw = resp.json().get("message", {}).get("content", "").strip()
                # Use regex to find the most likely JSON block containing personality keys
                json_match = re.search(r"(\{[\s\S]*?\"mood_bias\"[\s\S]*?\})", raw)
                if not json_match:
                    json_match = re.search(r"(\{[\s\S]*\})", raw)

                if json_match:
                    try:
                        return json.loads(json_match.group(0))
                    except json.JSONDecodeError:
                        pass
                return {}
        except Exception:
            pass
        return {}
