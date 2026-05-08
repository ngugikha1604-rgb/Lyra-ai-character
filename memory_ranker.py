import re
import requests

class MemoryRanker:
    """
    Ranks memory items based on their relevance to the current user input.
    Uses a lightweight model (e.g., qwen2.5:0.5b) for fast, numeric scoring.
    """
    def __init__(self):
        from config import LIGHT_MODEL, LIGHT_BASE_URL
        self.model = LIGHT_MODEL or "qwen2.5:0.5b"
        self.url = LIGHT_BASE_URL or "http://localhost:11434/api/chat"
        self._session = requests.Session()
        self._BATCH_SIZE = 20  # max items per LLM scoring call — prevents Ollama timeout on large inputs

    def _call_scoring_model(self, query: str, candidates: list[str]) -> list[float]:
        """Calls the light model to get relevance scores (1-10) for candidates."""
        if not candidates: return []
        prompt = f'Query: "{query}"\nRank how relevant each item is to the query (1-10, 10 is most relevant).\nReturn ONLY a comma-separated list of numbers.\nItems:\n'
        for i, cand in enumerate(candidates): prompt += f"{i + 1}. {cand[:120]}\n"

        try:
            resp = self._session.post(
                self.url,
                json={"model": self.model, "messages": [{"role": "user", "content": prompt}], "options": {"temperature": 0.1, "num_predict": 30}, "stream": False},
                timeout=4, verify=False
            )
            if resp.status_code == 200:
                content = resp.json().get("message", {}).get("content", "").strip()
                scores = [float(s.strip()) for s in re.findall(r"\b\d+\b", content)]
                if len(scores) < len(candidates): scores.extend([5.0] * (len(candidates) - len(scores)))
                return scores[:len(candidates)]
        except Exception:
            pass
        return None  # Signal failure — rank() will use weight-based saliency fallback

    def _score_in_batches(self, query: str, candidates: list[str]) -> list[float] | None:
        """Scores candidates in chunks of _BATCH_SIZE to avoid Ollama timeout on large inputs."""
        all_scores: list[float | None] = []
        any_success = False
        for i in range(0, len(candidates), self._BATCH_SIZE):
            batch = candidates[i:i + self._BATCH_SIZE]
            result = self._call_scoring_model(query, batch)
            if result is None:
                all_scores.extend([None] * len(batch))
            else:
                any_success = True
                all_scores.extend(result)
        return all_scores if any_success else None

    def rank(self, query: str, items: list[dict], token_budget: int = 550) -> list[str]:
        """Sorts items by Score * Weight and fits them into the token budget."""
        if not items: return []
        candidates_text = [i["value"] for i in items]
        scores = self._score_in_batches(query, candidates_text)
        
        scored_items = []
        for i, item in enumerate(items):
            if scores is not None and i < len(scores) and scores[i] is not None:
                relevancy = scores[i]
            else:
                # Fallback: weight đã encode saliency (weight * (1 + saliency/10) từ L1)
                # Scale lên ~1-10 để khớp với relevancy range của LLM scorer
                relevancy = item.get("weight", 1.0) * 5.0
            scored_items.append((relevancy * item.get("weight", 1.0), item["value"]))

        scored_items.sort(key=lambda x: x[0], reverse=True)
        result, current_chars = [], 0
        char_limit = token_budget * 3.2 # Lowered from 3.8 for safer Vietnamese estimation

        for _, text in scored_items:
            if current_chars + len(text) > char_limit: break
            result.append(text)
            current_chars += len(text)
        return result
