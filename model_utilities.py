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
    def __init__(self, model, base_url, session=None, timeout=60):
        super().__init__(session)
        self.model = model
        self.base_url = base_url
        self.timeout = timeout

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
            response = self.session.post(self.base_url, headers={"Content-Type": "application/json"}, json=data, timeout=self.timeout, verify=False)
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
                "top_p": 0.9,
                "stream": False
            }
            start = time.time()
            response = self.session.post(f"{ROUTER9_BASE_URL}/chat/completions", headers=headers, json=data, timeout=60, verify=False)
            if response.status_code == 200:
                raw = response.text.strip()
                if not raw:
                    print(f"[Router9] Empty response body for model={model}")
                    return None

                # Xử lý SSE stream (response bắt đầu bằng 'data: ')
                if raw.startswith("data:"):
                    content_parts = []
                    for line in raw.splitlines():
                        line = line.strip()
                        if not line.startswith("data:"):
                            continue
                        payload = line[5:].strip()
                        if payload == "[DONE]":
                            break
                        try:
                            chunk = __import__('json').loads(payload)
                            delta = chunk.get("choices", [{}])[0].get("delta", {})
                            content_parts.append(delta.get("content", ""))
                        except Exception:
                            continue
                    content = "".join(content_parts).strip()
                    if content:
                        print(f"[Router9 - {model}] Responded (stream) in {time.time() - start:.1f}s")
                        return content
                    print(f"[Router9] Empty stream content for model={model}")
                    return None

                # JSON thường
                try:
                    resp_json = __import__('json').loads(raw)
                except Exception as parse_err:
                    print(f"[Router9] JSON parse error for model={model}: {parse_err} | raw[:200]={raw[:200]}")
                    return None
                content = resp_json.get("choices", [{}])[0].get("message", {}).get("content", "")
                if content:
                    print(f"[Router9 - {model}] Responded in {time.time() - start:.1f}s")
                    return content.strip()
                else:
                    print(f"[Router9] Empty content for model={model}: {resp_json}")
            else:
                print(f"[Router9] Failed: {response.status_code} - {response.text[:300]}")
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
                'ollama_light': OllamaClient(LIGHT_MODEL, LIGHT_BASE_URL, self._session, timeout=45),
                'ollama_chat': OllamaClient(CHAT_MODEL, CHAT_BASE_URL, self._session, timeout=35),
                'openrouter': OpenRouterClient(self._session),
                'gemini': GeminiClient(self._session),
                'groq': GroqClient(self._session)
            }
        return self._llm_clients

    def _call_light_model(self, messages, temperature=0.3, max_tokens=200, provider="groq"):
        """Call light model qua 9router. Default provider là groq — nhanh và không cần Ollama."""
        # Nếu explicitly muốn Ollama local thì thử trước
        if provider == "ollama":
            res = self._clients['ollama_light'].call(messages, temperature=temperature, max_tokens=max_tokens)
            if res:
                return res
            # Ollama fail — fallback xuống 9router groq ngay, không thử các ollama khác
            return self._clients['router9'].call(messages, model=f"groq/{TRANSLATE_MODEL}", temperature=temperature, max_tokens=max_tokens)

        # Map provider sang model name đúng format của 9router
        model_map = {
            "ollama": f"ollama-local/{LIGHT_MODEL}",
            "openrouter": f"openrouter/{OPENROUTER_MODEL}",
            "gemini": f"gemini/{GEMINI_MODELS[0]}",
            "groq": f"groq/{TRANSLATE_MODEL}"
        }
        model = model_map.get(provider, f"groq/{TRANSLATE_MODEL}")
        res = self._clients['router9'].call(messages, model=model, temperature=temperature, max_tokens=max_tokens)
        if res: return res

        # Fallback thử các provider khác — chỉ dùng provider có trong 9router
        for fallback_model in [f"groq/{TRANSLATE_MODEL}", f"gemini/{GEMINI_MODELS[0]}", f"openrouter/{OPENROUTER_MODEL}"]:
            if fallback_model == model: continue  # bỏ qua model vừa fail
            res = self._clients['router9'].call(messages, model=fallback_model, temperature=temperature, max_tokens=max_tokens)
            if res: return res
        return None

    def _call_model(self, messages, temperature=0.8, max_tokens=250):
        """Call main chat model via 9router."""
        return self._call_chat_model(messages, temperature=temperature, max_tokens=max_tokens)

    def _compact_chat_messages(self, messages):
        user_msg = ""
        for message in reversed(messages or []):
            if message.get("role") == "user":
                user_msg = message.get("content", "")
                break
        return [
            {
                "role": "system",
                "content": (
                    "Bạn là Lyra, em gái VTuber người Việt. Bắt buộc xưng là 'em' và gọi người dùng là 'anh'. "
                    "Không xưng 'tôi', 'mình', không gọi người dùng là 'bạn' hoặc 'cậu', không nói mình là trợ lý AI. "
                    "Trả lời bằng tiếng Việt tự nhiên, ngắn gọn 1-2 câu. Ví dụ: 'Dạ, em đây anh.'"
                ),
            },
            {"role": "user", "content": user_msg},
        ]

    def _call_chat_model(self, messages, temperature=0.8, max_tokens=250):
        """Call main chat model qua 9router (Groq primary), fallback về qwen0.5b local nếu cần."""
        # 1. Primary: Groq qua 9router — nhanh, không tốn RAM local
        res = self._clients['router9'].call(messages, model=f"groq/{TRANSLATE_MODEL}", temperature=temperature, max_tokens=max_tokens)
        if res:
            return res

        # 2. Fallback: qwen0.5b local — nhẹ, chạy được khi stream VTS+OBS
        print("[Chat] Groq không available, fallback qwen0.5b local")
        compact_messages = self._compact_chat_messages(messages)
        res = self._clients['ollama_light'].call(compact_messages, temperature=temperature, max_tokens=min(max_tokens, 120))
        if res:
            return res

        # 3. Last resort: Gemini
        print("[Chat] qwen0.5b không available, thử Gemini")
        return self._clients['router9'].call(compact_messages, model=f"gemini/{GEMINI_MODELS[0]}", temperature=temperature, max_tokens=max_tokens)

    def _call_groq_model(self, messages, temperature=0.4, max_tokens=400):
        """Call Groq model qua 9router."""
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
            from ddgs import DDGS
            with DDGS() as ddgs:
                results = list(ddgs.text(query, max_results=max_results))
            if not results: return None
            formatted = [f"**{r.get('title', '')}**\n{r.get('body', '')[:200]}...\nSource: {r.get('href', '')}" for r in results if r.get('title') and r.get('body')]
            return "\n\n".join(formatted) if formatted else None
        except Exception as e:
            print(f"[Search] Error: {e}")
            return None

    def _clean_pronouns(self, text):
        """Cleans AI response from markers and quotes, and fixes pronouns."""
        if not text: return ""
        text = re.sub(r'["\']', '', text)
        text = re.sub(r'(?i)lyra:', '', text).strip()
        text = re.sub(r'\b[Tt]ôi\b', 'em', text)
        text = re.sub(r'\b[Tt]ớ\b', 'em', text)
        text = re.sub(r'\b[Tt]ao\b', 'em', text)
        text = re.sub(r'\b[Mm]ình\b', 'em', text)
        text = re.sub(r'\b[Bb]ạn\b', 'anh', text)
        text = re.sub(r'\b[Cc]ậu\b', 'anh', text)
        text = re.sub(r'\b[Mm]ày\b', 'anh', text)
        return text
