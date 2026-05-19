import os
from dotenv import load_dotenv

load_dotenv()

# ── Chat model (local Ollama) ──────────────────────────────────────────────────
USE_OLLAMA = True
CHAT_MODEL = "subsect/riko-qwen4b-q4:latest"
CHAT_BASE_URL = "http://localhost:11434/api/chat"
CHAT_FALLBACK_MODELS = []  # no fallback for local

# ── Light model (Ollama local) — dùng cho tác vụ phụ: memory extract, summarize, stream summary ──
# Dùng model nhỏ hơn để tiết kiệm Groq quota. Để trống để fallback về CHAT_MODEL.
LIGHT_MODEL = "qwen2.5:0.5b"   # VD: qwen2.5:0.5b, tinyllama, phi3:mini
LIGHT_BASE_URL = "http://localhost:11434/api/chat"  # Cùng Ollama endpoint

# ── Strong Model (Groq) — Primary brain for complex reasoning ──────────────────
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
STRONG_MODEL = "llama-3.3-70b-versatile"
LIGHT_GROQ_MODEL = "llama-3.1-8b-instant"
STRONG_BASE_URL = "https://api.groq.com/openai/v1/chat/completions"

# ── Async / Background Task Models (OpenRouter & Gemini) ──────────────────────
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL = "nvidia/nemotron-3-nano-30b-a3b:free"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1/chat/completions"

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODELS = ["gemini-2.5-flash", "gemini-2.5-flash-lite"]
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"

# ── 9router (Local Reverse Proxy) ──────────────────────────────────────────────
ROUTER9_BASE_URL = "http://localhost:20128/v1"
ROUTER9_API_KEY = os.environ.get("ROUTER9_API_KEY", "")  # 9router local không cần key

# ── Legacy aliases (kept so other parts of the code don't break) ──────────────
API_KEY = GROQ_API_KEY
BASE_URL = CHAT_BASE_URL
DEFAULT_MODEL = CHAT_MODEL
FALLBACK_MODELS = CHAT_FALLBACK_MODELS

MAX_HISTORY = 10
SUMMARY_TRIGGER = 15
MAX_SUMMARIES = 8

# ── TTS ────────────────────────────────────────────────────────────────────────
ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID", "")

# Edge TTS (Microsoft) — free, no API key, latency thấp hơn FPT nhiều
# Voices VI: vi-VN-HoaiMyNeural (nữ) | vi-VN-NamMinhNeural (nam)
EDGE_TTS_VOICE = "vi-VN-HoaiMyNeural"
EDGE_TTS_PITCH = "+50Hz"   # giọng trẻ hơn một chút, thay cho pydub pitch shift

# ── Misc ───────────────────────────────────────────────────────────────────────
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN", "")
FLASK_SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "lyra-default-secret")

STRONG_MODEL_ENABLED = True
SEARCH_ENABLED = True

# ── Stream Content ─────────────────────────────────────────────────────────────
# Chỉnh trước mỗi buổi stream. Để trống nếu không có nội dung cụ thể.
STREAM_TITLE = ""           # VD: "Farm Artifact Genshin Impact"
STREAM_GAME  = ""           # VD: "Genshin Impact"
STREAM_GOALS = []           # VD: ["Farm artifact", "Lên C2 Furina"]
STREAM_NOTES = ""           # VD: "Không spoil story, tập trung hype chat"

# ── Stream Queue Settings ──────────────────────────────────────────────────────
STREAM_REPLY_COOLDOWN      = 4.0   # giây giữa các response
STREAM_NEW_VIEWER_INTERVAL = 8.0   # giây giữa mỗi lần pick random viewer mới
STREAM_REGULAR_MIN_MESSAGES = 20   # số message tối thiểu để promote lên regular viewer
STREAM_SUMMARY_THRESHOLD    = 20   # tóm tắt stream sau mỗi N tin nhắn từ chat

# ── Chat Consensus Detection ───────────────────────────────────────────────────
CONSENSUS_EXCLAMATION_THRESHOLD = 0.30  # 30% unique senders → exclamation trigger
CONSENSUS_DISCUSSION_THRESHOLD  = 0.50  # 50% unique senders → discussion inject
CONSENSUS_COOLDOWN_SECONDS      = 60    # giây cooldown sau khi detect cùng topic
CONSENSUS_TOPIC_SHIFT_WINDOW    = 10    # giây để check topic shift trong cooldown

# ── Stream Owner (YouTube) ─────────────────────────────────────────────────────
OWNER_YOUTUBE_ID = os.environ.get("OWNER_YOUTUBE_ID", "")
YOUTUBE_VIDEO_ID = os.environ.get("YOUTUBE_VIDEO_ID", "")
YOUTUBE_LIVE_CHAT_ID = os.environ.get("YOUTUBE_LIVE_CHAT_ID", "")
YOUTUBE_CHANNEL_ID = os.environ.get("YOUTUBE_CHANNEL_ID", "")

# ── Vector database Key ──────────────────────────────────────────────────────
PINECONE_API_KEY = os.environ.get("PINECONE_API_KEY", "")
PINECONE_INDEX   = "lyra-memory"   # tên index trên Pinecone free tier

# ── Embedding model (Ollama local) — dùng thay SentenceTransformer để tiết kiệm RAM ──
# Cần pull model trước: ollama pull nomic-embed-text
EMBEDDING_MODEL  = "nomic-embed-text"
EMBEDDING_URL    = "http://localhost:11434/api/embeddings"  # Ollama embeddings endpoint

# ── Reflection Settings ────────────────────────────────────────────────────────
REFLECTION_INTERVAL = 20  # turns
