# Lyra AI — VTuber Companion System

Lyra is a high-fidelity conversational AI designed to feel like a real companion. She's characterized as a 16-year-old younger sister with a distinct Vietnamese persona, emotional depth, and a multi-layered memory system.

**Core Goal**: Evolve into a fully autonomous VTuber persona capable of long-term relationship building and adaptive stream performance.

---

## 🗂️ Project Structure

```
lyra-bot/
├── main.py                    # Entry point (Flask dev server)
├── core.py                    # MiniAI orchestrator (Mixin-based)
├── config.py                  # All constants, API keys, model configs
├── AGENTS.md                  # Technical notes for agents/developers
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

## 🏗️ Core Architecture

The system uses a **Mixin-based Orchestrator** in `core.py` to manage complexity while keeping the `MiniAI` class lean.

```python
class MiniAI(
    BehavioralMixin,        # behavioral_logic.py
    PromptBuilderMixin,     # prompt_builder.py
    StreamHandlerMixin,     # stream_handler.py
    MemoryHandlerMixin,     # memory_handler.py
    ModelUtilityMixin,      # model_utilities.py
):
```

`MiniAI.chat()` is the single entry point for all interactions. It handles intent detection, emotion updates, memory retrieval, prompt building, brain calls, and post-turn persistence.

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

**Agentic loops inside `_call_and_parse_vbrain`**:
- **Skill Loop**: If the brain returns a `skill_needed`, loads the Markdown skill and re-calls the brain (max 2 iterations).
- **VTS Feedback Loop**: After executing VTS tools, runtime errors are fed back as `[FEEDBACK]` for self-correction (max 1 iteration).
- **Parallel Context**: `_collect_turn_context()` uses a `ThreadPoolExecutor` to fetch memory and web search concurrently.

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

- Uses `dspy.ChainOfThought(LyraChatSignature)` — generates a `rationale` (internal monologue) before the final reply.
- Loaded from `lyra_compiled.json` if available (pre-optimized weights via DSPy compiler).
- Routed through **9router** (`localhost:20128`) using LiteLLM-compatible format: `openai/groq/<model>`.
- Falls back to `_call_model()` + `parse_vbrain_response()` if DSPy init fails.

---

## 🌐 Multi-Model Backend

| Role | Provider | Model | Config Key |
|------|----------|-------|------------|
| **Primary (Brain)** | Ollama local | `subsect/riko-qwen4b-q4:latest` | `CHAT_MODEL` |
| **Light tasks** (memory extract, summarize) | Ollama local | `qwen2.5:0.5b` | `LIGHT_MODEL` |
| **DSPy Brain / Strong Model** | Groq (via 9router) | `llama-3.3-70b-versatile` | `STRONG_MODEL` |
| **Background / Async tasks** | OpenRouter | `nvidia/nemotron-3-nano-30b-a3b:free` | `OPENROUTER_MODEL` |
| **Reflection / Planning** | Google Gemini | `gemini-2.5-flash` | `GEMINI_MODELS[0]` |
| **Embeddings** | Ollama local | `nomic-embed-text` | `EMBEDDING_MODEL` |

**9router** (`localhost:20128`) is a local reverse proxy that routes LiteLLM-format model strings (e.g. `openai/groq/llama-3.3-70b-versatile`) to the correct upstream provider. All DSPy calls go through 9router.

---

## 🧬 Memory System

Three-layer cognitive architecture mimicking human neurobiology:

| Layer | Type | Storage | Role |
|-------|------|---------|------|
| **L1** | Semantic | SQLite | Persistent facts about the user, core personality, learned skills |
| **L2** | Working | In-Memory/JSON | Current stream state, transient events (donations), recent turns |
| **L3** | Episodic | Pinecone | Long-term vector-indexed memories for semantic search (RAG) |

### Memory Pipeline

1. **Heuristic Extraction** — Fast keyword/regex extraction for names and preferences.
2. **AI Extraction** — LLM-based structured transcription (`LIGHT_MODEL` via Ollama).
3. **Conflict Resolution** — New facts compared against L1; contradictions mark old facts as `superseded`.
4. **Consolidation (CLS)** — "Sleep-phase" distillation: high-reward episodic memories promoted to L1 Semantic core.

Key files: `memory.py`, `memory_handler.py`, `memory_consolidator.py`, `memory_ranker.py`, `pinecone_layer.py`, `live_context.py`.

---

## 🎭 Emotion Engine 2.0

Lyra operates in a **3D VAD Space** rather than simple sentiment scores:

- **Valence (Mood)**: `[-10, +10]` — how good or bad she feels.
- **Arousal (Attention)**: `[0, 10]` — energy level and focus.
- **Dominance**: `[0.0, 1.0]` — confidence and control in the conversation.

### Mechanics

- **Cognitive Appraisal** (Lazarus, 1991): Messages evaluated for *Congruence* and *Control*, scaling mood/dominance shifts.
- **Hydraulic Model** (Lorenz): `irritability` accumulates and triggers an `outburst` at `> 0.85`, causing a sudden mood/dominance drop.
- **Homeostasis**: Mood decays toward 0 (neutral) at 8%/turn.
- **VAD → Tokens**: `emotion.get_dynamic_max_tokens()` returns longer replies at high arousal.
- **VAD → Temperature**: `conv_state.get_temperature()` scales temperature with mood/attention/dominance.

---

## 🧠 Behavioral Psychology Layers

Five modules in `BehavioralMixin` (`behavioral_logic.py`):

1. **Variable Ratio Reinforcement (Skinner)** — 7% base chance to trigger "Rewards" (Deep Recalls, Vulnerability, Curiosity Spikes) for unpredictable engagement.
2. **Speech Act Classifier (Austin/Searle)** — Classifies user intent (Expressive, Directive, Commissive, etc.) to adjust Lyra's perlocutionary goal.
3. **LSM Tracker (Giles/Pennebaker)** — Tracks Linguistic Style Matching. Lyra may *converge* to mirror the user or *diverge* to assert dominance.
4. **Self-Disclosure Engine (Walther)** — Controlled release of Lyra's internal thoughts to deepen trust (requires `affection >= 50`).
5. **Active Inference**:
   - **Ideological Proactivity** — Chance to push a philosophical question (guarded: skips if user mood is sad/stressed/anxious or state is `closing`).
   - **Predictive Surprise** (~5%) — Subverts current emotional patterns for unpredictability.

---

## 🔄 Conversation State Machine

`ConversationStateDetector` (`conversation_state.py`) tracks flow across 6 states:

```
greeting → normal → deepening ↔ shifting ↔ conflict → resolution
```

- **Pace control**: `get_pace_max_tokens()` — shorter replies during `shifting`/`conflict`.
- **Temperature control**: `get_temperature()` — warmer during `deepening`, cooler during `conflict`.
- **Reward gating**: `should_trigger_reward()` — only fires in stable states.

---

## 🎙️ Communication & VTuber Integration

### VTube Studio Bridge (`vts_api.py`)

Controls the Live2D avatar via WebSocket:

| Parameter | Mapping |
|-----------|---------|
| `ParamBrowLY/RY` | `valence * 0.4` |
| `ParamEyeL/ROpen` | `0.76 + arousal * 1.14` |
| `ParamBodyAngleX` | `(dominance - 0.5) * 10` |

- Idle swaying and random blinking after 5s of inactivity.
- `EXP_THINKING` expression triggers immediately on user input.
- VTS errors are injected back into the brain as `[FEEDBACK]` (Agent-Zero pattern).

### TTS & Paralinguistics (`app/services/audio_service.py`)

- **FPT AI** converts text to speech with dynamic speed mapping.
- `attention` level influences TTS speed (`-2` for tired, `+1` for excited).
- Pitch shifted with `octaves=0.22` for a youthful "younger sister" timbre.
- Audio routed via **VB-Cable** for OBS capture.
- `proactive_service` checks the audio queue before triggering proactive messages.

---

## 📺 Stream Layer

### Flask Application Factory (`app/__init__.py`)

`create_app()` registers 6 blueprints and injects all dependencies as `app.*` attributes:

- Blueprints: `chat_bp`, `tts_bp`, `stream_bp`, `stream_events_bp`, `auth_bp`, `admin_bp`
- Dependencies: `lyra_ai`, `viewer_tracker`, `chat_analyzer`, `yt_poller`, `ai_chat_lock`, `vts_bridge`, `audio_service`, `sse_service`, `stream_service`
- Rate limits: 30/min chat, 20/min TTS, 60/min stream
- Filesystem-backed sessions (`flask-session`) with 1-year lifetime

### Viewer Tracking (`viewer_tracker.py`)

- **`ViewerTracker`**: Per-viewer SQLite profiles (message count, affinity score, last seen). Promotes to `regular` after `STREAM_REGULAR_MIN_MESSAGES` (default 20) messages.
- **`ChatPatternAnalyzer`**: Detects consensus events:
  - `exclamation` trigger: ≥30% unique senders using exclamation patterns
  - `discussion` trigger: ≥50% unique senders on same topic
  - 60s cooldown between triggers on the same topic
- **Emoji Interpretation**: `emoji_meanings.json` translates emojis into semantic context.

### YouTube Integration (`youtube_chat.py`)

`YouTubeChatPoller` polls the YouTube Live Chat API and routes messages through `ViewerTracker` → `stream_service` → `lyra_ai.chat(source_type="viewer")`.

**`/stream/start` auto-detection flow** (priority order):
1. Use `chat_id` from request body or env `YOUTUBE_LIVE_CHAT_ID`
2. Use `video_id` from request or env `YOUTUBE_VIDEO_ID` → call `get_live_chat_id()`
3. If neither → call `get_current_live_stream_info()` to auto-find the active stream
4. If still no chat_id → return 400 with a clear message

The frontend sends `POST /stream/start` with body `{}` — no manual ID input needed.

**Dependency**: `google-api-python-client` must remain installed.

### SSE Events (`app/services/sse_service.py`)

Broadcasts real-time events to browser clients (emotion, action, reply, VTS state). Max 10 concurrent subscribers.

---

## 📈 Self-Evolving System

- **RL Feedback Loop** (`rl_feedback_loop.py`): 15s reward window after Lyra speaks on stream. Scores responses from -10 to +10 based on viewer chat.
- **Post-Stream Review**: High-reward patterns promoted to Pinecone (`rl_few_shot`) and distilled into `live_context.json`.
- **Skill Synthesizer** (`skill_synthesizer.py`): Generates new Markdown-based skills in `/skills/` from successful interactions. Skills with `protected=True` are never cleaned up.
- **Reflection Loop** (`_reflect_on_session()`): Every `REFLECTION_INTERVAL` turns (default 20), calls Gemini to generate 2-3 "Thấu hiểu" (insights) pushed to `live_context.json`.
- **Stream Plan** (`_generate_stream_plan()`): At stream start, calls OpenRouter to generate dynamic session goals from `STREAM_TITLE`, `STREAM_GOALS`, and `STREAM_NOTES`.

---

## ⚙️ Background Worker

All non-critical I/O goes through a single centralized priority queue with 4 daemon worker threads (`background_worker.py`).

| Priority | Constant | Use cases |
|----------|----------|-----------|
| 1 | `PRIORITY_CRITICAL` | Memory extraction (owner data) |
| 2 | `PRIORITY_HIGH` | Stream summary, emotion refresh, session items |
| 3 | `PRIORITY_NORMAL` | Consolidation, history summarization, DB save |

```python
from background_worker import enqueue, PRIORITY_CRITICAL, PRIORITY_HIGH, PRIORITY_NORMAL
enqueue(PRIORITY_NORMAL, some_function, arg1, arg2)
```

---

## 🚀 Setup & Running

### Prerequisites

- Python 3.10+
- [Ollama](https://ollama.ai) running locally with models: `subsect/riko-qwen4b-q4:latest`, `qwen2.5:0.5b`, `nomic-embed-text`
- [9router](https://github.com/9router) running on `localhost:20128`
- VB-Cable (for audio routing to OBS)
- VTube Studio with WebSocket API enabled (optional)

### Environment

Copy `.env.example` to `.env` and fill in:

```
GROQ_API_KEY=...
OPENROUTER_API_KEY=...
GEMINI_API_KEY=...
PINECONE_API_KEY=...
FPT_API_KEY=...
```

### Install

```bash
pip install -r requirements.txt
```

### Run

```bash
python main.py
```

The Flask dev server starts on `http://localhost:5000`. Open the admin panel at `/admin` to manage stream settings and monitor state.

### YouTube OAuth

Navigate to `/auth/login` to complete the YouTube OAuth flow. Credentials are stored in `client_secret.json` and reused across sessions.

---
