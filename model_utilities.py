import re
import time
import requests
from config import (
    GROQ_API_KEY,
    LIGHT_MODEL, CHAT_MODEL, LIGHT_BASE_URL, CHAT_BASE_URL,
    TRANSLATE_MODEL, TRANSLATE_BASE_URL, SEARCH_ENABLED,
    OPENROUTER_API_KEY, OPENROUTER_MODEL, OPENROUTER_BASE_URL,
    GEMINI_API_KEY, GEMINI_MODELS, GEMINI_BASE_URL,
    ROUTER9_BASE_URL, ROUTER9_API_KEY
)

class BaseLLMClient:
    def __init__(self, session=None):
        self.session = session or requests.Session()

    def call(self, messages, temperature=0.8, max_tokens=250):
        raise NotImplementedError

class OllamaClient(BaseLLMClient):
    def __init__(self, model, base_url, session=None):
        super().__init__(session)
        self.model = model
        self.base_url = base_url

    def call(self, messages, temperature=0.8, max_tokens=250):
        if not self.model or not self.base_url: return None
        try:
            data = {
                "model": self.model,
                "messages": messages,
                "options": {"temperature": temperature, "num_predict": max_tokens, "num_ctx": 4096, "top_p": 0.9},
                "stream": False,
            }
            start = time.time()
            response = self.session.post(self.base_url, headers={"Content-Type": "application/json"}, json=data, timeout=60, verify=False)
            if response.status_code == 200:
                content = response.json().get("message", {}).get("content", "").strip()
                if content:
                    print(f"[Ollama - {self.model}] Responded in {time.time() - start:.1f}s")
                    return content
            else:
                print(f"[Ollama] Failed ({response.status_code})")
        except Exception as e:
            print(f"[Ollama] Error: {e}")
        return None

class OpenRouterClient(BaseLLMClient):
    def call(self, messages, temperature=0.8, max_tokens=250):
        if not OPENROUTER_API_KEY: return None
        headers = {
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://lyra-ai.local",
            "X-Title": "Lyra AI"
        }
        data = {
            "model": OPENROUTER_MODEL,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "top_p": 0.9
        }
        try:
            start = time.time()
            response = self.session.post(OPENROUTER_BASE_URL, headers=headers, json=data, timeout=60)
            if response.status_code == 200:
                resp_json = response.json()
                content = resp_json.get("choices", [{}])[0].get("message", {}).get("content", "")
                if content:
                    content = content.strip()
                    print(f"[OpenRouter] Responded in {time.time() - start:.1f}s")
                    return content
                else:
                    print(f"[OpenRouter] Empty content. Full response: {resp_json}")
            else:
                print(f"[OpenRouter] Failed: {response.status_code} - {response.text}")
        except Exception as e:
            print(f"[OpenRouter] Error: {e}")
        return None

class GeminiClient(BaseLLMClient):
    _current_idx = 0

    def call(self, messages, temperature=0.8, max_tokens=250):
        if not GEMINI_API_KEY or not GEMINI_MODELS: return None
        
        # Luân phiên model để tránh lỗi quota 429
        model = GEMINI_MODELS[GeminiClient._current_idx % len(GEMINI_MODELS)]
        GeminiClient._current_idx += 1

        headers = {
            "Authorization": f"Bearer {GEMINI_API_KEY}",
            "Content-Type": "application/json"
        }
        data = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "top_p": 0.9
        }
        try:
            start = time.time()
            response = self.session.post(GEMINI_BASE_URL, headers=headers, json=data, timeout=60)
            if response.status_code == 200:
                content = response.json().get("choices", [{}])[0].get("message", {}).get("content", "").strip()
                if content:
                    print(f"[Gemini - {model}] Responded in {time.time() - start:.1f}s")
                    return content
            else:
                print(f"[Gemini - {model}] Failed: {response.status_code} - {response.text}")
        except Exception as e:
            print(f"[Gemini] Error: {e}")
        return None

class Router9Client(BaseLLMClient):
    """Central client for all LLM calls via 9router reverse proxy."""
    
    def call(self, messages, model="auto", temperature=0.8, max_tokens=250):
        if not ROUTER9_BASE_URL: return None
        try:
            headers = {"Content-Type": "application/json"}
            if ROUTER9_API_KEY:
                headers["Authorization"] = f"Bearer {ROUTER9_API_KEY}"
            data = {
                "model": model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "top_p": 0.9
            }
            start = time.time()
            response = self.session.post(f"{ROUTER9_BASE_URL}/chat/completions", headers=headers, json=data, timeout=60, verify=False)
            if response.status_code == 200:
                resp_json = response.json()
                content = resp_json.get("choices", [{}])[0].get("message", {}).get("content", "")
                if content:
                    print(f"[Router9 - {model}] Responded in {time.time() - start:.1f}s")
                    return content.strip()
                else:
                    print(f"[Router9] Empty content: {resp_json}")
            else:
                print(f"[Router9] Failed: {response.status_code} - {response.text}")
        except Exception as e:
            print(f"[Router9] Error: {e}")
        return None

class GroqClient(BaseLLMClient):
    def call(self, messages, temperature=0.8, max_tokens=250):
        if not GROQ_API_KEY: return None
        headers = {
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type": "application/json"
        }
        backoff = 2.0
        for attempt in range(3):
            try:
                data = {"model": TRANSLATE_MODEL, "messages": messages, "temperature": temperature, "max_tokens": max_tokens, "top_p": 0.9}
                start_time = time.time()
                response = self.session.post(TRANSLATE_BASE_URL, headers=headers, json=data, timeout=60, verify=False)
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

class ModelUtilityMixin:
    """
    Mixin for LLM calling, translation, and web search utilities.
    """
    _session = requests.Session()

    @property
    def _clients(self):
        if not hasattr(self, '_llm_clients'):
            self._llm_clients = {
                'router9': Router9Client(self._session),
                'ollama_light': OllamaClient(LIGHT_MODEL, LIGHT_BASE_URL, self._session),
                'ollama_chat': OllamaClient(CHAT_MODEL, CHAT_BASE_URL, self._session),
                'openrouter': OpenRouterClient(self._session),
                'gemini': GeminiClient(self._session),
                'groq': GroqClient(self._session)
            }
        return self._llm_clients

    def _call_light_model(self, messages, temperature=0.3, max_tokens=200, provider="ollama"):
        """Call internal model via 9router (supports ollama, openrouter, gemini, groq)."""
        # Map provider sang model name cho LiteLLM/9router format
        model_map = {
            "ollama": f"ollama-local/{LIGHT_MODEL}",
            "openrouter": f"openrouter/{OPENROUTER_MODEL}",
            "gemini": f"gemini/{GEMINI_MODELS[0]}",
            "groq": f"groq/{TRANSLATE_MODEL}"
        }
        model = model_map.get(provider, f"ollama/{LIGHT_MODEL}")
        res = self._clients['router9'].call(messages, model=model, temperature=temperature, max_tokens=max_tokens)
        if res: return res
        
        # Fallback thử các provider khác
        for fallback_model in [f"ollama/{LIGHT_MODEL}", f"gemini/{GEMINI_MODELS[0]}", f"openrouter/{OPENROUTER_MODEL}"]:
            res = self._clients['router9'].call(messages, model=fallback_model, temperature=temperature, max_tokens=max_tokens)
            if res: return res
        return None

    def _call_model(self, messages, temperature=0.8, max_tokens=250):
        """Call main chat model via 9router."""
        return self._call_chat_model(messages, temperature=temperature, max_tokens=max_tokens)

    def _call_chat_model(self, messages, temperature=0.8, max_tokens=250):
        """Call main chat model via 9router (Groq primary)."""
        return self._clients['router9'].call(messages, model=f"groq/{TRANSLATE_MODEL}", temperature=temperature, max_tokens=max_tokens)

    def _call_groq_model(self, messages, temperature=0.4, max_tokens=400):
        """Call Groq model via 9router."""
        return self._clients['router9'].call(messages, model=f"groq/{TRANSLATE_MODEL}", temperature=temperature, max_tokens=max_tokens)

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