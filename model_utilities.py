import re
import time
import requests
import json
from prompts import TRANSLATE_PROMPT
from config import (
    GROQ_API_KEY,
    LIGHT_MODEL, CHAT_MODEL, LIGHT_BASE_URL, CHAT_BASE_URL,
    TRANSLATE_MODEL, TRANSLATE_BASE_URL, SEARCH_ENABLED
)

class ModelUtilityMixin:
    """
    Mixin for LLM calling, translation, and web search utilities.
    """
    _session = requests.Session()

    def _call_light_model(self, messages, temperature=0.3, max_tokens=200):
        """Call Ollama light model for internal tasks."""
        model = LIGHT_MODEL or CHAT_MODEL
        url = LIGHT_BASE_URL or CHAT_BASE_URL
        if not model:
            return self._call_model(messages, temperature=temperature, max_tokens=max_tokens)

        try:
            data = {
                "model": model,
                "messages": messages,
                "options": {"temperature": temperature, "num_predict": max_tokens, "num_ctx": 2048, "top_p": 0.9},
                "stream": False,
            }
            start = time.time()
            response = self._session.post(url, headers={"Content-Type": "application/json"}, json=data, timeout=20, verify=False)
            duration = time.time() - start
            if response.status_code != 200:
                print(f"[Light] Ollama failed ({response.status_code}), falling back")
                return self._call_model(messages, temperature=temperature, max_tokens=max_tokens)
            content = response.json().get("message", {}).get("content", "").strip()
            if content:
                print(f"[Light] Responded in {duration:.1f}s")
                return content
        except Exception as e:
            print(f"[Light] Error: {e}, falling back")
        return self._call_model(messages, temperature=temperature, max_tokens=max_tokens)

    def _call_model(self, messages, temperature=0.8, max_tokens=200):
        """Call local Ollama chat model."""
        return self._call_chat_model(messages, temperature=temperature, max_tokens=max_tokens)

    def _call_chat_model(self, messages, temperature=0.8, max_tokens=200):
        """Call Groq primary, fallback to local Ollama."""
        result = self._call_groq_model(messages, temperature=temperature, max_tokens=max_tokens)
        if result: return result

        print("[Chat] Groq unavailable, falling back to Ollama...")
        for attempt in range(2):
            try:
                data = {
                    "model": CHAT_MODEL,
                    "messages": messages,
                    "options": {"temperature": temperature, "num_predict": max_tokens, "num_ctx": 4096, "top_p": 0.9, "repeat_penalty": 1.1},
                    "stream": False,
                }
                start_time = time.time()
                response = self._session.post(CHAT_BASE_URL, headers={"Content-Type": "application/json"}, json=data, timeout=getattr(self, 'timeout', 60), verify=False)
                duration = time.time() - start_time
                if response.status_code == 200:
                    content = response.json().get("message", {}).get("content", "").strip()
                    if content:
                        print(f"[Chat] Ollama responded in {duration:.1f}s")
                        return content
            except Exception as e:
                print(f"[Chat] Ollama error: {e}")
        return None

    def _call_groq_model(self, messages, temperature=0.4, max_tokens=150):
        """Call Groq primary chat model."""
        backoff = 2.0
        for attempt in range(3):
            try:
                data = {"model": TRANSLATE_MODEL, "messages": messages, "temperature": temperature, "max_tokens": max_tokens, "top_p": 0.9}
                start_time = time.time()
                response = self._session.post(TRANSLATE_BASE_URL, headers=self._translate_headers, json=data, timeout=60, verify=False)
                duration = time.time() - start_time
                if response.status_code == 429:
                    wait = max(float(response.headers.get("retry-after", backoff)), backoff)
                    print(f"[Groq] Rate limited. Waiting {wait:.1f}s...")
                    time.sleep(wait)
                    backoff = min(backoff * 2, 30.0)
                    continue
                if response.status_code == 200:
                    content = response.json().get("choices", [{}])[0].get("message", {}).get("content", "").strip()
                    if content:
                        print(f"[Chat] Groq responded in {duration:.1f}s")
                        return content
                else:
                    print(f"[Chat] Groq failed ({response.status_code})")
                    break
            except Exception as e:
                print(f"[Groq] Error: {e}")
        return None

    def _translate_response(self, text):
        """Standardizes translation if needed, currently no-op."""
        return text

    def _should_search(self, user_input):
        """Determines if a web search is needed."""
        if not SEARCH_ENABLED: return False, None
        user_lower = user_input.lower()
        question_patterns = ["what is", "who is", "when", "where", "why", "how", "tìm", "kiếm", "là gì", "ai là", "ở đâu", "mới nhất", "tin tức", "thời tiết", "giá"]
        if any(p in user_lower for p in question_patterns) and not any(p in user_lower for p in ["my", "i'm", "i am", "we", "tôi", "mình"]):
            return True, user_input
        return False, None

    def _search_web(self, query, max_results=3):
        """Performs web search using DuckDuckGo."""
        try:
            from duckduckgo_search import DDGS
            with DDGS() as ddgs:
                results = list(ddgs.text(query, max_results=max_results))
            if not results: return None
            formatted = [f"**{r.get('title', '')}**\n{r.get('body', '')[:200]}...\nSource: {r.get('href', '')}" for r in results if r.get('title') and r.get('body')]
            return "\n\n".join(formatted) if formatted else None
        except Exception as e:
            print(f"[Search] Error: {e}")
            return None

    def clean_reply(self, text):
        """Cleans AI response from markers and quotes."""
        if not text: return ""
        text = re.sub(r'["\']', '', text)
        text = re.sub(r'(?i)lyra:', '', text).strip()
        return text
