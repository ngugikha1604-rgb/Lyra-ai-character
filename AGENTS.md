# AGENTS.md — Lyra AI Project

## 🧠 Project Overview

This is a conversational AI system named **Lyra**.

Lyra is not a generic chatbot. She has:

* A persistent personality (like a younger sister)
* Emotional state (mood, affection, attention)
* Long-term memory stored in SQLite
* Time-aware behavior (Vietnam timezone)
* Natural, short, human-like texting style

Core goal:

> Build a **real-feeling AI companion**, with the ultimate long-term goal of evolving into a fully autonomous **VTuber persona**.

---

## 🏗️ Core Architecture

### 1. Main Engine

* File: `core.py`
* Class: `MiniAI`

Handles:

* Conversation loop
* Prompt building
* Memory system
* Emotion system
* Time-based behavior

---

### 2. Memory System (IMPORTANT)

Storage:

* SQLite (`memory.db`)
* Migrated from old JSON (`memory.json`)

Types of memory:

* Profile (name, age, location…)
* Preferences (likes / dislikes)
* Goals
* Topics
* Episodic summaries
* Relational notes

Key features:

* Saliency scoring (importance of memory)
* Memory buffer (lazy extraction → save cost)
* Auto summarization (compress old chats)
* Retrieval based on relevance (not dump all memory)
* **Memory Forgetting**: Stale SQLite memory items (`saliency < 7`, `access_count = 0`) are permanently deleted after 100 turns to save context length.

---

### 3. Personality System

Defined in:

* `BASE_PERSONALITY`
* `NATURAL_BASE_PERSONALITY`

Key traits:

* 16-year-old little sister vibe
* Casual, short, reactive texting
* Not always playful (context-aware)
* No “AI-like” behavior

Rules:

* No overexplaining
* No fake enthusiasm
* No repetitive patterns
* Adjust tone based on user mood

---

### 4. Emotion System

Variables:

* `mood` (-10 → +10)
* `affection` (0 → 100)
* `attention` (0 → 10)

Effects:

* Changes tone of responses
* Influences behavior (playful vs calm)
* Persisted partially (affection)
* **Fatigue System**: `attention` drains per chat (-0.3) and recovers during silence (+2.0/hr). Low attention triggers tired, short responses.
* **Emotion Decay**: Long periods of silence (>12h) decay `mood` by 50% towards 0 (neutral).
* **Affection Cap**: Caps relationship spikes at +/- 5 per turn for natural progression.

---

### 5. Time Awareness

* Uses Vietnam timezone
* Detects:

  * Morning / afternoon / evening / night
  * Time gap between messages

Features:

* Time-based personality shifts
* Smart greetings
* Proactive messages after inactivity

---

### 6. Memory Extraction Pipeline

Flow:

1. Heuristic extraction (cheap)
2. Buffer candidates
3. Periodic AI extraction (batch)
4. Save to SQLite

Goal:

* Reduce API usage
* Keep only meaningful memory

---

### 7. Conversation Management

* Stores last ~40 messages
* Summarizes old messages automatically
* Keeps:

  * Recent context
  * Compressed long-term memory

---

### 8. Psychological Tweaks & Dynamic Persona

Advanced features implemented to make Lyra "human":
* **Weekend Context**: Time-aware prompts dynamically switch her to a "lazy/gaming" vibe on weekends.
* **Proactive Curiosity**: 15% random chance to override her response and force her to proactively ask the user about past `goals` or `topics`.
* **Dynamic Persona Tiers**: Prompt injections override behavior based on affection (<30: cold/distant, 30-75: teasing/normal, >75: clingy/demanding).
* **Dynamic Auto-Tokens**: Modifies the `max_tokens` API param dynamically. If she's tired (`attention < 3`), it locks to 40 max tokens (short, cold texts). If energized, it expands to 180 tokens.

---

### 9. Conversation State Machine

File: `conversation_state.py` — `ConversationStateDetector`

States: `greeting → building → deepening → shifting → closing → goodbye`

Features:
* **Rhythm Detection**: Tracks avg user message length (rolling window 10 turns) → injects length hint into prompt
* **Dynamic Temperature**: Maps emotion state + conversation state to LLM temperature (0.60–1.10)
  * closing/goodbye → 0.60 (safe, predictable)
  * deepening → 0.75
  * bored/angry → +0.10 (rawer responses)

---

### 10. Dual Model Setup

| Model | Role | Endpoint |
|-------|------|----------|
| `llama-3.3-70b-versatile` (Groq) | Primary chat | `https://api.groq.com/openai/v1/chat/completions` |
| `subsect/riko-qwen4b-q4:latest` (Ollama local) | Chat fallback | `http://localhost:11434/api/chat` |
| `LIGHT_MODEL` (Ollama local, e.g. `qwen2.5:0.5b`) | Internal tasks only | `http://localhost:11434/api/chat` |

- Groq is tried first (fast, cloud). If Groq fails/times out → automatic fallback to local Ollama.
- **Light model** (`_call_light_model()`) handles all internal tasks: memory extraction, summarization, mega compression, stream chat summary. Timeout 20s, fallback to `_call_model` if unavailable.
- Groq quota is reserved exclusively for main chat replies.
- Groq 429 rate limit → exponential backoff (reads `retry-after` header, doubles each retry, cap 30s), only falls back to Ollama after 3 failed retries.

---

### 11. Thought Chaining

- After generating a reply, if `monologue` is substantial (>20 chars) and `random() < 0.07` (7% chance), Lyra calls the model a second time.
- The monologue from the first call is passed as "prior thought" → model develops a more natural continuation.
- Uses `THOUGHT_CHAIN_SYSTEM` as a dedicated system prompt (not `build_prompt()`).
- Only applies to `source_type="owner"` chat — never for viewers.

---

### 12. Per-Situation Prompt System

Each situation uses a dedicated prompt instead of one shared system prompt:

| Prompt | Used in |
|--------|---------|
| `NATURAL_BASE_PERSONALITY` | Owner private chat |
| `STREAM_VIEWER_PERSONALITY` | All viewer chat (regular/new/donor) |
| `THOUGHT_CHAIN_SYSTEM` | Thought chaining second call |
| `STREAM_EVENT_SYSTEM` | Stream events (start/stop/milestone) |
| `STREAM_GREETING_PROMPT` | Stream start greeting |
| `STREAM_FAREWELL_PROMPT` | Stream stop farewell |
| `PROACTIVE_STREAM_PROMPT` | Silence fill during stream |
| `REGULAR_VIEWER_ARRIVAL_HINT` | Injected when regular viewer sends first message of session |
| `MEMORY_EXTRACT_SYSTEM` | Memory extraction |
| `SUMMARIZE_PROMPT` | Conversation summarization |
| `MEMORY_COMPRESSION_PROMPT` | Mega summary compression |

`STREAM_VIEWER_PERSONALITY` makes Lyra aware she is streaming to an audience — not in a private 1-1 conversation. Keeps replies to 1 sentence max.

---

## 🌐 Web Layer

* Frontend: `index.html`
* Backend: Flask server (`web.py`)

Responsibilities:

* Send user input → MiniAI
* Return AI response
* Render Live2D emotion / UI
* TTS via FPT AI API
* Speech-to-Text via Web Speech API (vi-VN)
* YouTube Live Chat integration (stream mode)

Note:

> Web layer is thin. All intelligence is in `core.py`.

---

## 📡 YouTube Streaming Architecture

### Overview

Lyra operates in two modes:
- **Private mode** — Owner talks directly via STT in browser (`/chat` route, `source_type="owner"`)
- **Stream mode** — YouTube Live Chat messages are polled and processed (`/stream-chat`, `_handle_stream_event`)

### Files Involved

| File | Role |
|------|------|
| `youtube_chat.py` | Polls YouTube Live Chat API, scores messages, pushes to internal queue |
| `viewer_tracker.py` | Tracks viewer stats, manages `regular_viewers` table, builds stream context |
| `web.py` | Priority queue consumer, SSE broadcast, stream control routes |
| `config.py` | Stream settings (content, cooldowns, thresholds) |

---

### Source Types

Every `chat()` call carries a `source_type` that changes Lyra's behavior:

| source_type | Who | Affection | Memory saved? |
|-------------|-----|-----------|---------------|
| `owner` | Streamer via STT | From `memory.db` | ✅ Full |
| `regular_viewer` | Known viewer | From `regular_viewers` table | ❌ |
| `new_viewer` | Unknown viewer | Fixed at 10 | ❌ |
| `donor` | Super Chat sender | Boosted +20 temporarily | ❌ |

**Critical rule:** Only `owner` chat saves to `memory.db`. Viewer chat never pollutes the owner's long-term memory.

---

### Priority Queue System

Messages from YouTube are processed in strict priority order:

```
Tier 1: donor        → Queue maxsize=20,  processed first always
Tier 2: regular_viewer → Queue maxsize=50
Tier 3: new_viewer   → Random pool maxsize=100, pick 1 every N seconds
```

Owner (STT) bypasses the queue entirely — processed immediately via `/chat`.

Config in `config.py`:
- `STREAM_REPLY_COOLDOWN` — seconds between replies (default 4.0s)
- `STREAM_NEW_VIEWER_INTERVAL` — seconds between random new_viewer picks (default 8.0s)
- `STREAM_REGULAR_MIN_MESSAGES` — messages needed to promote to regular (default 20)

---

### Regular Viewer System

**Per-stream session tracking** (in-memory, `viewer_stats` table):
- Every message increments `message_count` and recalculates `affinity_score` (log scale)
- No message content stored for privacy — only counts

**Promotion** (triggered on `/stream/stop`):
- Viewers with `message_count >= STREAM_REGULAR_MIN_MESSAGES` → promoted to `regular_viewers` table
- Returning regulars: `affection += 5` per stream (capped at 85), `total_streams += 1`
- New regulars: start at `affection = 30`

**`regular_viewers` table schema:**
```sql
viewer_id, platform, viewer_name, total_streams, total_messages,
affection (0-100), first_seen, last_seen, notes
```

---

### Stream Content Context

Set in `config.py` before each stream:
```python
STREAM_TITLE = "Farm Artifact Genshin"
STREAM_GAME  = "Genshin Impact"
STREAM_GOALS = ["Farm artifact", "Lên C2 Furina"]
STREAM_NOTES = "Không spoil story"
```

Injected into every prompt as `[STREAM CONTEXT]` block. Lyra always knows what the stream is about. Also injects top 3 stream milestones from `stream_milestones` table.

---

### Donate Detection

`youtube_chat.py` detects `superChatEvent` and `superStickerEvent` from YouTube API:
- Sets `is_donor=True` and `donate_amount` on the event
- Pushed to `donor` queue with priority=10 (highest)
- Lyra reacts with warm acknowledgment and reads the donor's name

---

### SSE (Server-Sent Events)

Frontend subscribes to `/stream/events` to receive Lyra's replies in real-time:
- Each reply is broadcast to all connected clients
- Includes: `reply`, `emotion`, `action`, `sender_name`, `source_type`, `affection`, `mood`
- Heartbeat every 20s to keep connection alive

---

### Stream Control Routes

| Route | Method | Description |
|-------|--------|-------------|
| `/stream/start` | POST | Start YouTube polling (requires OAuth) |
| `/stream/stop` | POST | Stop polling + promote regular viewers |
| `/stream/status` | GET | Current poller status + queue stats |
| `/stream/content` | GET | Current stream content from config |
| `/stream/analytics` | GET | Top viewers, regulars, queue stats |
| `/stream/viewers/regulars` | GET | List all regular viewers |
| `/stream/events` | GET | SSE endpoint for frontend |
| `/authorize` | GET | Start YouTube OAuth flow |
| `/oauth2callback` | GET | OAuth callback |

---

### Chat Pattern Analyzer

`ChatPatternAnalyzer` in `viewer_tracker.py` tracks chat vibe:
- Collects top words and emojis from all messages (batched every 10 msgs)
- Builds `[Chat style]` hint injected into Lyra's prompt
- Triggers stream summary every 30 messages (AI summarizes chat vibe → saved to episodic memory)
- Style hint is cached in-memory, only rebuilt after a DB flush (not every message)

---

### Stream Events

`generate_stream_event_reply(event_type, context)` in `core.py` handles non-viewer stream moments:

| event_type | Trigger | Prompt used |
|------------|---------|-------------|
| `greeting` | `/stream/start` | `STREAM_GREETING_PROMPT` |
| `farewell` | `/stream/stop` | `STREAM_FAREWELL_PROMPT` (with summary + top viewers) |
| `milestone` | Manual / milestone check | Inline description |
| `silence_fill` | Chat silent >30s | `PROACTIVE_STREAM_PROMPT` |

All events use `_call_light_model()` — output is 1-2 sentences, no need for Groq quota. Result is broadcast via SSE to frontend.

---

### Regular Viewer Arrival

When a regular viewer sends their first message of a stream session:
- `_handle_stream_event` detects `tier == "regular_viewer"` + `message_count == 1`
- `_greeted_viewers_this_session` set (protected by `_greeted_lock`) prevents double-greeting
- `REGULAR_VIEWER_ARRIVAL_HINT` is injected into `stream_ctx` with viewer name, total streams, affection
- Lyra may naturally greet them — not forced, depends on her mood
- Set is cleared on `/stream/stop` for the next session

---

### Stream Milestones

`stream_milestones` table in `memory.db` tracks one-time stream achievements:
- `check_stream_milestone(event_type, description)` — inserts only if `event_type` not yet recorded (UNIQUE constraint)
- Checked on `/stream/stop`: debut, stream #10, #25, #50, #100
- Top 3 milestones injected into `[STREAM CONTEXT]` block so Lyra can reference them naturally

---

### TTS Pipeline

1. Lyra generates reply text
2. Frontend calls `POST /speak` with the reply text
3. Backend calls FPT AI TTS API (`voice: banmai`)
4. FPT returns async URL → backend fetches MP3 → streams to frontend
5. Frontend plays audio with lip sync via Web Audio API analyser

---

### Speech-to-Text (Owner Input)

- Uses browser Web Speech API (`vi-VN`)
- Click 🎤 button → starts listening → interim text shown in input box
- On speech end → auto-sends message to `/chat` as `source_type="owner"`
- Stops current TTS playback before listening to avoid feedback loop

---

## ⚙️ AI Behavior Rules (VERY IMPORTANT)

When modifying or extending this project:

### DO:

* Keep responses short and natural
* Prioritize realism over correctness
* Use memory subtly (not dump info)
* Let personality emerge, not be forced

### DO NOT:

* Turn Lyra into a generic assistant
* Add robotic explanations
* Overuse emojis or reactions
* Break conversational flow

---

## 🧪 Common Tasks for Agents

### Add new feature

→ Modify `MiniAI` class only

### Improve memory

→ Work in:

* `memory_items`
* saliency logic
* retrieval functions

### Improve personality

→ Adjust:

* `NATURAL_BASE_PERSONALITY`
* NOT hardcoded responses

### Debug conversation issues

→ Check:

* intent detection
* memory context injection
* emotion update

---

## 🚀 Design Philosophy

This project is built around:

> “Less AI, more human.”

* Imperfection > robotic perfection
* Natural flow > structured replies
* Memory feeling > memory accuracy

---

## ⚠️ Constraints

* Must remain lightweight (student environment)
* Minimize API usage
* Avoid expensive multi-call pipelines
* Prefer heuristics + batching

---

## 📌 Summary for AI Agents

If you are an AI working on this project:

* You are not building a chatbot
* You are maintaining a **character with memory and emotion**
* Every change must preserve:

  * personality
  * natural conversation
  * lightweight performance

---
