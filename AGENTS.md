# AGENTS.md — Lyra AI Project (Architectural Specification)

## 🧠 Project Overview
Lyra is a high-fidelity conversational AI system designed to be a "real-feeling" companion. She is characterized as a 16-year-old younger sister with a distinct Vietnamese persona, emotional depth, and a multi-layered memory system.

**Core Goal**: Evolve into a fully autonomous VTuber persona capable of long-term relationship building and adaptive stream performance.

---

## 🗂️ Project Structure

```
lyra-bot/
├── main.py                    # Entry point (Flask dev server)
├── core.py                    # MiniAI orchestrator (Mixin-based)
├── config.py                  # All constants, API keys, model configs
├── AGENTS.md                  # Architectural specification (this file)
├── PLAN.md                    # Bug tracker & fix plan
│
├── app/                       # Flask application factory
│   ├── __init__.py            # create_app(), blueprint registration, DI
│   ├── middleware.py          # Security headers
│   ├── helpers.py             # Shared route utilities
│   ├── routes/
│   │   ├── chat.py            # POST /chat (owner chat endpoint)
│   │   ├── tts.py             # POST /speak (TTS synthesis)
│   │   ├── stream.py          # Stream start/stop/viewer management
│   │   ├── stream_events.py   # SSE event push (/stream/events)
│   │   ├── auth.py            # YouTube OAuth flow
│   │   └── admin.py           # Admin panel routes
│   └── services/
│       ├── audio_service.py   # TTS queue & VB-Cable playback
│       ├── sse_service.py     # Server-Sent Events broadcast
│       ├── stream_service.py  # Stream orchestration (YouTube + viewer replies)
│       └── proactive_service.py # Proactive message timer
│
├── dspy_modules/
│   ├── brain_module.py        # LyraBrain (dspy.Module with ChainOfThought)
│   └── signatures.py         # LyraChatSignature (structured I/O contract)
├── lyra_compiled.json         # Compiled/optimized DSPy brain weights
│
├── behavioral_logic.py        # BehavioralMixin
├── prompt_builder.py          # PromptBuilderMixin (4-tier token budget)
├── stream_handler.py          # StreamHandlerMixin
├── memory_handler.py          # MemoryHandlerMixin
├── model_utilities.py         # ModelUtilityMixin (multi-model calls)
│
├── emotion.py                 # EmotionEngine (VAD + Hydraulic model)
├── memory.py                  # MemorySystem (SQLite L1/L2 + session)
├── memory_handler.py          # Memory extraction & injection helpers
├── memory_consolidator.py     # CLS sleep-phase consolidation
├── memory_ranker.py           # Importance scoring for memories
├── memory_utils.py            # Shared DB helpers
├── pinecone_layer.py          # L3 Episodic memory (Pinecone vector DB)
│
├── conversation_state.py      # ConversationStateDetector (6-state FSM)
├── lyra_brain.py              # parse_vbrain_response, validate_emotion/action
├── rl_feedback_loop.py        # RLFeedbackLoop (viewer reward scoring)
├── skill_synthesizer.py       # SkillSynthesizer (generate & manage /skills)
├── viewer_tracker.py          # ViewerTracker + ChatPatternAnalyzer
├── youtube_chat.py            # YouTubeChatPoller (Live Chat API)
├── vts_api.py                 # VTube Studio WebSocket bridge
├── background_worker.py       # Centralized priority job queue (4 workers)
├── live_context.py            # Live stream ephemeral context (L2)
├── time_utils.py              # Vietnam timezone helpers
├── prompts.py                 # Static prompt constants (REWARD_HINTS, etc.)
├── discord_bot.py             # Optional Discord integration
│
├── skills/                    # Markdown skill files (synthesized at runtime)
├── live_context.json          # Ephemeral stream state (game, focus, plan)
└── memory.db                  # SQLite database (WAL mode)
```

---

## 🏗️ Core Architecture (Mixin-Based)
The system uses a **Mixin-based Orchestrator** in `core.py` to manage complexity while keeping the `MiniAI` class manageable.

```python
class MiniAI(
    BehavioralMixin,        # behavioral_logic.py
    PromptBuilderMixin,     # prompt_builder.py
    StreamHandlerMixin,     # stream_handler.py
    MemoryHandlerMixin,     # memory_handler.py
    ModelUtilityMixin,      # model_utilities.py
):
```

- **Entry Point**: `chat(user_input, source_type="owner", viewer_data=None, stream_context="")` adalah satu-satunya titik masuk.
- **Agentic Logic** (in `_call_and_parse_vbrain`):
    - **Skill Loop**: If the DSPy Brain returns a `skill_needed`, the system loads the Markdown skill and re-calls the brain (max 2 iterations).
    - **VTS Feedback Loop**: After executing VTS tools, any runtime errors are fed back to the brain as `[FEEDBACK]` for self-correction (max 1 iteration).
- **Parallel Context Collection**: `_collect_turn_context()` uses a `ThreadPoolExecutor` to fetch memory context and web search results concurrently.

### `chat()` Execution Flow
```
chat(user_input)
  ├── _refresh_time_state()
  ├── detect_intent() + detect_user_mood()
  ├── emotion.update()
  ├── _track_stream_turn()          → L2 session memory
  ├── _apply_viewer_emotion_context()
  ├── _collect_turn_context()       → parallel: memory + web search
  ├── _build_reward_hint()          → Variable Ratio Reinforcement
  ├── _choose_active_inference()    → ideology / surprise trigger
  ├── build_prompt()                → 4-tier token budget
  ├── _build_api_messages()
  ├── _call_and_parse_vbrain()
  │     ├── DSPy Brain (primary) or _call_model() fallback
  │     ├── Skill Loop (if skill_needed)
  │     └── VTS Feedback Loop (if VTS error)
  ├── clean_reply() + _maybe_add_filler()
  ├── _restore_viewer_emotion_context()
  ├── _enqueue_memory_extraction()  → background
  ├── _persist_owner_turn()         → SQLite + in-memory
  └── rl_loop.register_action()     → viewer RL scoring
```

---

## 🤖 DSPy Brain Layer
Lyra's core response generation uses **DSPy** (Declarative Self-improving Python) instead of raw prompt strings. This separates *what to output* (signatures) from *how to route* (LM config).

### `LyraChatSignature` (`dspy_modules/signatures.py`)
Defines the structured I/O contract for all LLM calls:

| Field | Type | Description |
|-------|------|-------------|
| `persona` | Input | Core personality, address rules, behavioral constraints |
| `situation` | Input | Current context (time, stream state, Lyra's mood) |
| `memory` | Input | Relevant memories about the user |
| `chat_history` | Input | Recent conversation turns |
| `user_message` | Input | Current message from owner/viewer |
| `emotion` | Output | Expression: `neutral\|happy\|sad\|angry\|thinking\|ecstatic\|bored` |
| `action` | Output | VTS action: `WAVE\|NOD\|SHAKE_HEAD\|THINK\|LAUGH\|NONE` |
| `skill_needed` | Output | Skill name or `NONE` |
| `reply` | Output | Response text (max ~300 chars) |

### `LyraBrain` (`dspy_modules/brain_module.py`)
- Uses `dspy.ChainOfThought(LyraChatSignature)` — generates a `rationale` (monologue/thinking) before the final reply.
- Loaded from `lyra_compiled.json` if available (pre-optimized weights via DSPy compiler).
- Routed through **9router** (`localhost:20128`) using LiteLLM-compatible format: `openai/groq/<model>`.

### Fallback Behavior
If DSPy init fails or a call errors, `_call_and_parse_vbrain()` falls back to a standard `_call_model()` call and parses the raw text response via `lyra_brain.parse_vbrain_response()`.

---

## 🌐 Multi-Model Backend
The system uses multiple LLM providers with distinct roles:

| Role | Provider | Model | Config Key |
|------|----------|-------|------------|
| **Primary (Brain)** | Ollama local | `subsect/riko-qwen4b-q4:latest` | `CHAT_MODEL` |
| **Light tasks** (memory extract, summarize) | Ollama local | `qwen2.5:0.5b` | `LIGHT_MODEL` |
| **DSPy Brain / Translate / Polish** | Groq (via 9router) | `llama-3.3-70b-versatile` | `TRANSLATE_MODEL` |
| **Background / Async tasks** | OpenRouter | `nvidia/nemotron-3-nano-30b-a3b:free` | `OPENROUTER_MODEL` |
| **Reflection / Planning** | Google Gemini | `gemini-2.5-flash` | `GEMINI_MODELS[0]` |
| **Embeddings** | Ollama local | `nomic-embed-text` | `EMBEDDING_MODEL` |

**9router** (`localhost:20128`) acts as a local reverse proxy that routes LiteLLM-format model strings (e.g. `openai/groq/llama-3.3-70b-versatile`) to the correct upstream provider. All DSPy calls go through 9router.

> **Note for agents**: Use `_call_model()` for primary chat, `_call_light_model(provider=...)` for background tasks. Never hardcode API keys or base URLs — always read from `config.py`.

---

## 🧬 Memory System (Three-Layer Cognitive Architecture)
Lyra mimics human neurobiology using a hybrid storage approach (SQLite + Pinecone).

| Layer | Type | Storage | Role |
|-------|------|---------|------|
| **L1** | **Semantic** | SQLite | Persistent facts about the user, core personality, and learned skills. |
| **L2** | **Working** | In-Memory/JSON | Current stream state, transient events (donations), and recent turns. |
| **L3** | **Episodic** | Pinecone | Long-term vector-indexed memories for semantic search (RAG). |

### Memory Pipeline:
1. **Heuristic Extraction**: Fast keyword/regex extraction for names/preferences.
2. **AI Extraction**: LLM-based structured transcription of memories (`LIGHT_MODEL` via Ollama).
3. **Conflict Resolution**: New facts are compared against L1. Contradictions mark old facts as `superseded`.
4. **Consolidation (CLS)**: "Sleep-phase" distillation where high-reward episodic memories are promoted to L1 Semantic core.

### Key Files
- `memory.py` — `MemorySystem`: main class, SQLite I/O, session items, embedding calls.
- `memory_handler.py` — `MemoryHandlerMixin`: `extract_memory()`, `get_relevant_context()` injected into `MiniAI`.
- `memory_consolidator.py` — CLS consolidation logic.
- `memory_ranker.py` — Importance scoring.
- `pinecone_layer.py` — L3 vector upsert/query.
- `live_context.json` — Ephemeral stream state (game, focus, viewer insights, plan). Managed by `live_context.py`.

---

## 🎭 Emotion Engine 2.0 (VAD & Hydraulic Models)
Lyra doesn't use simple sentiment scores. She operates in a **3D VAD Space**:

- **Valence (Mood)**: [-10, +10] — How "good" or "bad" she feels.
- **Arousal (Attention)**: [0, 10] — Energy level and focus.
- **Dominance**: [0.0, 1.0] — Level of confidence and control in the conversation.

### Advanced Mechanics:
- **Cognitive Appraisal**: Incoming messages are evaluated for *Congruence* and *Control* (Lazarus, 1991), scaling mood/dominance shifts.
- **Hydraulic Model (Lorenz)**: `irritability` accumulates during annoying interactions and triggers an `outburst` when it exceeds 0.85, causing a sudden drop in mood and dominance.
- **Homeostasis**: Mood naturally decays towards 0 (neutral) at 8%/turn.
- **VAD → Dynamic tokens**: `emotion.get_dynamic_max_tokens()` returns longer replies when arousal is high.
- **VAD → Temperature**: `conv_state.get_temperature()` scales temperature with mood/attention/dominance.

---

## 🧠 Behavioral Psychology Layers
Five specialized modules in `BehavioralMixin` (`behavioral_logic.py`):

1. **Variable Ratio Reinforcement (Skinner)**: 7% base chance to trigger "Rewards" (Deep Recalls, Vulnerability, Curiosity Spikes) to keep engagement unpredictable.
2. **Speech Act Classifier (Austin/Searle)**: Classifies user intent (Expressive, Directive, Commissive, etc.) to adjust Lyra's "Perlocutionary" goal.
3. **LSM Tracker (Giles/Pennebaker)**: Tracks Linguistic Style Matching. Lyra may *converge* to mirror the user or *diverge* to assert dominance.
4. **Self-Disclosure Engine (Walther)**: Controlled release of Lyra's "internal" thoughts to deepen relationship trust (requires `affection >= 50`).
5. **Active Inference**:
    - **Ideological Proactivity**: Chance to push a philosophical question (guarded: skips if user mood is sad/stressed/anxious or conversation state is `closing`).
    - **Predictive Surprise** (~5%): Subverts current emotional patterns to create unpredictability.

---

## 🔄 Conversation State Machine
`ConversationStateDetector` (`conversation_state.py`) tracks conversation flow across 6 states:

`greeting` → `normal` → `deepening` ↔ `shifting` ↔ `conflict` → `resolution`

- **Pace control**: `get_pace_max_tokens()` — shorter replies during `shifting`/`conflict`.
- **Temperature control**: `get_temperature()` — warmer during `deepening`, cooler during `conflict`.
- **Reward gating**: `should_trigger_reward()` — only fires rewards in stable states.

---

## 🎙️ Communication & VTuber Integration

### VTube Studio (VTS) Bridge (`vts_api.py`)
Lyra controls her Live2D avatar via a dedicated WebSocket bridge.
- **VAD Mapping**:
    - `ParamBrowLY/RY` = `valence * 0.4`
    - `ParamEyeL/ROpen` = `0.76 + arousal * 1.14`
    - `ParamBodyAngleX` = `(dominance - 0.5) * 10`
- **Idle Behavior**: Automatic swaying and random blinking after 5s of inactivity.
- **Thinking State**: Triggers `EXP_THINKING` expression immediately upon receiving user input.
- **Error Feedback**: `_execute_vts_tools()` returns error strings; these are injected back into the brain as `[FEEDBACK]` (Agent-Zero pattern).

### TTS & Paralinguistics (`app/services/audio_service.py`)
- **FPT AI Integration**: Converts text to speech with dynamic speed mapping.
- **Prosody Mapping**: `attention` level influences TTS speed (`-2` for tired, `+1` for excited).
- **Pitch Shifting**: Post-processes audio with `octaves=0.22` to achieve a more youthful "younger sister" timbre.
- **Audio Routing**: Outputs via `VB-Cable` to allow OBS capture for streaming.
- **Queue**: `audio_service` manages a playback queue; `proactive_service` checks the queue before triggering proactive messages.

---

## 📺 Stream Layer

### Flask Application Factory (`app/__init__.py`)
The web layer uses an **Application Factory** pattern (`create_app()`):
- Registers 6 blueprints: `chat_bp`, `tts_bp`, `stream_bp`, `stream_events_bp`, `auth_bp`, `admin_bp`.
- Initializes all dependencies and injects them as `app.*` attributes: `lyra_ai`, `viewer_tracker`, `chat_analyzer`, `yt_poller`, `ai_chat_lock`, `vts_bridge`, `audio_service`, `sse_service`, `stream_service`.
- Applies rate limiting via `flask-limiter`: 30/min for chat, 20/min for TTS, 60/min for stream.
- Uses filesystem-backed sessions (`flask-session`) with 1-year lifetime.

### Viewer Tracking (`viewer_tracker.py`)
- **`ViewerTracker`**: Maintains per-viewer profiles in SQLite (message count, affinity score, last seen). Promotes viewers to `regular` status after `STREAM_REGULAR_MIN_MESSAGES` (default 20) messages.
- **`ChatPatternAnalyzer`**: Detects consensus events in chat:
    - `exclamation` trigger: ≥30% unique senders using exclamation patterns.
    - `discussion` trigger: ≥50% unique senders on same topic.
    - Cooldown: 60s between triggers on the same topic.
- **Emoji Interpretation**: Loads `emoji_meanings.json` to translate emojis into semantic context before injecting into stream context.

### YouTube Integration (`youtube_chat.py`)
- `YouTubeChatPoller` polls the YouTube Live Chat API and routes messages through `ViewerTracker` → `stream_service` → `lyra_ai.chat(source_type="viewer")`.
- **`get_current_live_stream_info(credentials_dict)`** — Tự động lấy `(video_id, live_chat_id)` của buổi stream đang active trên kênh bằng `liveBroadcasts().list(broadcastStatus="active")`. **Không cần user nhập gì.**
- **`get_live_chat_id(credentials_dict, video_id)`** — Lấy `live_chat_id` từ một `video_id` cụ thể qua `videos().list(part="liveStreamingDetails")`.
- **Dependency**: `google-api-python-client` (`pip install google-api-python-client`). Package này đã được cài sẵn và **không được xóa khỏi requirements**.
- **Flow của `/stream/start`** (theo thứ tự ưu tiên):
  1. Dùng `chat_id` nếu có trong request body hoặc env `YOUTUBE_LIVE_CHAT_ID`.
  2. Dùng `video_id` nếu có (từ request hoặc env `YOUTUBE_VIDEO_ID`) → gọi `get_live_chat_id()` để lấy chat_id.
  3. Nếu không có gì → gọi `get_current_live_stream_info()` để tự tìm stream đang live.
  4. Nếu vẫn không tìm được chat_id → trả 400 với message rõ ràng.
- **Frontend `index.html`** KHÔNG cần input Video ID — nút "Bắt đầu Stream" chỉ gửi `POST /stream/start` với body `{}`. Backend tự resolve.

> ⚠️ **AGENTS: Đừng xóa `get_current_live_stream_info` hay `get_live_chat_id` khỏi `youtube_chat.py`.** Đừng thêm input field Video ID vào frontend. Đừng thay đổi flow auto-detect ở `/stream/start`. Đây là thiết kế cố ý — user không cần nhập ID thủ công.

### SSE Events (`app/services/sse_service.py`)
- Broadcasts events to connected browser clients (UI updates: emotion, action, reply, VTS state).
- Max 10 concurrent SSE subscribers.

---

## 📈 Self-Evolving System (RLHF & Skills)
- **RL Feedback Loop** (`rl_feedback_loop.py`): Opens a 15s "Reward Window" after Lyra speaks on stream. Captures viewer chat to score the response (-10 to +10). Uses `background_worker.enqueue()` instead of raw `threading.Timer`.
- **Post-Stream Review**: High-reward patterns are promoted to Pinecone (`rl_few_shot`) and distilled into `live_context.json` constraints for future sessions.
- **Skill Synthesizer** (`skill_synthesizer.py`): Periodically analyzes successful interactions to generate new, repeatable Markdown-based skills in `/skills/`. Skills can be marked `protected=True` to prevent cleanup.
- **Reflection Loop** (`_reflect_on_session()`): Every `REFLECTION_INTERVAL` turns (default 20), calls `GEMINI` to generate 2-3 high-level "Thấu hiểu" (insights) about the session and pushes them to `live_context.json` via `update_insights()`.
- **Stream Plan** (`_generate_stream_plan()`): At stream start, calls `OPENROUTER` to generate dynamic session goals from `STREAM_TITLE`, `STREAM_GOALS`, and `STREAM_NOTES`.

---

## ⚙️ Background Worker (`background_worker.py`)
All non-critical I/O goes through a single **centralized priority queue** with 4 daemon worker threads. Never use `threading.Thread` or `threading.Timer` directly for background tasks.

| Priority | Constant | Use cases |
|----------|----------|-----------|
| 1 | `PRIORITY_CRITICAL` | Memory extraction (owner data) |
| 2 | `PRIORITY_HIGH` | Stream summary, emotion refresh, session items |
| 3 | `PRIORITY_NORMAL` | Diary, consolidation, history summarization, DB save |

```python
from background_worker import enqueue, PRIORITY_CRITICAL, PRIORITY_HIGH, PRIORITY_NORMAL
enqueue(PRIORITY_NORMAL, some_function, arg1, arg2)
```

---

## 🛠️ Operational Guidelines for Agents

1. **Invariants**:
    - Lyra luôn xưng "em", gọi owner là "anh". Không bao giờ xưng "tôi" hay gọi user bằng tên lạ.
    - `MiniAI.chat()` là nơi duy nhất quyết định `reward_hint` và `active_inference_mode` — không di chuyển logic này sang Mixin.
    - `_skill_loop_count` và `_vts_loop_count` phải được reset về 0 ở đầu mỗi lượt `chat()`.

2. **Context Management**:
    - Use the **4-tier greedy token budget** in `PromptBuilderMixin`.
    - `live_context.json` là ephemeral stream data (donations, focus, insights, plan). Không dùng cho long-term facts.
    - `_stream_ctx` phải ở **TIER 1** (cùng với `base_personality`) để không bị drop khi memory context lớn.

3. **Background Tasks**:
    - Dùng `enqueue(PRIORITY, func)` từ `background_worker.py` cho mọi I/O không cần đồng bộ.
    - Không tạo `threading.Thread`, `threading.Timer`, hoặc `ThreadPoolExecutor` mới ngoài `core.py`.

4. **State Machine**:
    - Tôn trọng 6 conversation states: `greeting`, `normal`, `deepening`, `shifting`, `conflict`, `resolution`.
    - Không trigger `ideology` hoặc `surprise` khi state là `closing`/`goodbye` hoặc user mood là `sad`/`stressed`.

5. **Multi-Model Usage**:
    - Primary replies → `_call_model()` (Ollama local).
    - Memory extraction / summarize → `_call_light_model(provider="ollama")` (`qwen2.5:0.5b`).
    - Reflection / planning → `_call_light_model(provider="gemini"` hoặc `"openrouter"`).
    - Không gọi Groq trực tiếp cho chat — chỉ DSPy Brain qua 9router.

6. **DSPy Brain**:
    - Khi thêm output field mới, cập nhật cả `LyraChatSignature` lẫn `_parse_brain_result()`.
    - Sau khi optimize brain (DSPy compiler), lưu lại `lyra_compiled.json`.
    - Fallback về `parse_vbrain_response()` nếu DSPy unavailable — đảm bảo hàm này parse được mọi format output.

7. **Database**:
    - Mọi SQLite write phải qua `DB_LOCK` từ `memory.py`.
    - DB chạy WAL mode (`PRAGMA journal_mode=WAL`) — kiểm tra file `.db-wal` và `.db-shm` là bình thường.
    - Không query `get_analytics()` hay `get_history()` ngoài `with DB_LOCK:`.

---
*Created by Antigravity — Standardized for Lyra AI character development.*
