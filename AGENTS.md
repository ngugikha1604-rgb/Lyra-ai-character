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

### 2. Memory System (Advanced Cognitive Architecture)

Lyra uses a structured, three-layer memory system inspired by human neurobiology to balance personality consistency, short-term awareness, and long-term history.

**Storage**: Hybrid (SQLite for structure + Pinecone for vector search).

#### Memory Layers:
| Layer | Psychological Equivalent | Storage | Injection Logic |
|-------|--------------------------|---------|-----------------|
| **L1 — Semantic** | Shared Long-term Memory (facts, traits, goals) | SQLite | Always available; injected via Ranker |
| **L2 — Working Memory** | Short-term Awareness (current session events) | In-memory | Active during session; clears on 'sleep' |
| **L3 — Episodic** | Temporal/Autobiographical context (past events) | Pinecone | Semantic search (RAG) retrieval only |

#### Cognitive Components:

*   **Working Memory & Attention Control (Ranking Module)**:
    - Instead of simple FIFO, Lyra uses an **Attention Control** module.
    - A light model (`qwen2.5:0.5b`) acts as a reranker, scoring candidates for relevancy.
    - **Token Budgeting**: Only top items fitting a ~550 token window are "attended" to, mimicking human focus.
*   **Complementary Learning Systems (CLS - Consolidation)**:
    - Mimics the **sleep-phase consolidation** of the human brain.
    - Triggered after every stream session (`/stream/stop`).
    - **Distillation**: Analyzes today's episodic buffer to detect recurring patterns.
    - **Integration**: Stable findings are migrated from L2/L3 to L1 (Semantic Memory).
    - **Personality Adaptation**: Updates behavioral indices (mood bias, affection rate) based on daily vibe.
*   **Layered Retrieval**: Prevents context bloat by only pulling what's necessary.
*   **Conflict Resolution**: Detects contradictions (e.g., changing likes) and updates facts while archiving historical changes to L3.
*   **Automatic Consolidation**: Stale L1 items are moved or deleted to keep the database clean.

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

### 6. Memory Extraction & Conflict Pipeline

Flow:

1. **Heuristic extraction**: Fast keyword-based detection.
2. **Buffer candidates**: Items are staged to check for frequency and saliency.
3. **AI extraction (Batch)**: Light model cleans and extracts structured JSON.
4. **Conflict Resolution**: New facts are compared against existing L1 items. If a contradiction is found, the old item is marked `superseded` and archived to L3.
5. **Storage**: User facts save to SQLite; Temporal/Episodic events upsert to Pinecone for long-term RAG.

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
*   **Working Memory (Attention Control)**: A ranking module prioritized context based on semantic relevance, allowing her to stay "on topic" during complex discussions.
*   **Sleep-phase Consolidation (CLS)**: Post-stream distillation of episodic events into semantic core traits, enabling her personality to evolve naturally over time.
*   **Weekend Context**: Time-aware prompts dynamically switch her to a "lazy/gaming" vibe on weekends.
*   **Dynamic Persona Tiers**: Prompt injections override behavior based on affection (<30: cold/distant, 30-75: teasing/normal, >75: clingy/demanding).
*   **Dynamic Auto-Tokens**: Base `max_tokens` from `EmotionEngine.get_dynamic_max_tokens()` (35/70/100 based on attention), then scaled by `ConversationStateDetector.get_pace_max_tokens()` based on user's avg message length. Clamps to [30, 180]. Scale-up only allowed when Lyra is not tired (base >= 70).

---

### 8a. 🧠 Psychological Layer (PLAN.md — Implemented)

Four psychological systems layered on top of the base engine. All live in `conversation_state.py` + `core.py`. **Owner-only** unless noted.

#### 2. Dopaminergic Feedback Loop
Variable Ratio Reinforcement — keeps engagement unpredictable.

*   **Deep Recalls** (`get_rare_memory()` in `memory.py`): 7% chance per turn (via `should_trigger_reward()`), cooldown 3 turns. Fetches a high-saliency (`>= 3`), least-accessed L1 memory item and injects `[SURPRISE REWARD]` into system prompt. Lyra references it naturally.
*   **Healthy Debate**: Fallback when no rare memory found. Injects `[SURPRISE REWARD: HEALTHY DEBATE]` — Lyra pushes back on user's opinion instead of agreeing.
*   **Filler Words** (`_maybe_add_filler()` in `core.py`): 12% chance, only when user asks a complex/reflective question (regex match on Vietnamese + English keywords). Prepends "hmmm...", "à thì...", etc. to reply. Applied AFTER `clean_reply()`, BEFORE saving to `original_reply` (so DB history stays clean).

#### 3. Cognitive Entrainment (Mirroring)
Lyra synchronizes her style and pace to the user.

*   **Vibe Sync** (`get_vibe_tier()` → `get_rhythm_hint()`): Tracks `_slang_count` and `_intellectual_count` per turn (additive +2 on hit, -1 decay on miss). Tier thresholds: `>= 4` → `"slang"` or `"intellectual"`. Injects `[VIBE SYNC]` instruction into system prompt via `conv_hints`.
*   **Pace Sync** (`get_pace_max_tokens()`): Adjusts token limit based on user's rolling avg message length. Scale-down freely (mirror brevity), scale-up only when Lyra is energized.
*   **Temperature Sync** (`get_temperature()`): Slang tier → +0.08 temp (raw, casual). Intellectual tier → -0.08 temp (precise, consistent). Layered on top of state + emotion adjustments.

#### 4. Active Inference & Epistemic Foraging
Lyra proactively disrupts predictable patterns.

All decisions are made at **one single point** in `chat()` before calling `build_prompt()` and `compose_user_message()`. Priority order: `reward > ideology > surprise` — never 2 modes in the same turn.

*   **Ideological Proactivity** (`should_trigger_ideology()`): 15% chance (internal roll), cooldown 5 turns, no-repeat tracking across session (cycles after all 8 prompts used). Requires `attention >= 4`. When triggered: `compose_user_message()` returns early with `[CURIOSITY RULE: OVERRIDE REPLY]` + `user_input` prepended so model still sees the original message.
*   **Predictive Surprise** (`should_trigger_surprise()`): 5% chance, cooldown 5 turns. Injects `[PREDICTIVE SURPRISE]` into system prompt — Lyra subverts her current mood pattern (happy → cold, angry → warm, neutral → extreme). Cross-cooldown: when ideology triggers, `_last_surprise_turn` is reset to prevent back-to-back active inference turns.

**Key invariants to preserve:**
- `reward_hint` blocks both ideology and surprise (set in `chat()` before the Active Inference block).
- `_ideology_idx` and `active_inference_mode` are computed once and passed to both `build_prompt(active_inference_mode=...)` and `compose_user_message(ideology_idx=...)`.
- Never call `should_trigger_ideology()` or `should_trigger_surprise()` inside `build_prompt()` or `compose_user_message()` — they must only be called from `chat()`.

---

### 8b. 🧬 Emotion Architecture Upgrade (PLAN.md — 4/5 Implemented)

Advanced emotion model layered on top of `EmotionEngine`. All changes live in `emotion.py` unless noted.

#### VAD Model (Valence-Arousal-Dominance) ✅ Done

`EmotionEngine` now operates on a 3D emotion space:

| Variable | Range | Role |
|----------|-------|------|
| `mood` | -10 → +10 | Valence proxy (primary) |
| `attention` | 0 → 10 | Arousal proxy (primary) |
| `dominance` | 0.0 → 1.0 | NEW — confidence/control level |

New properties and methods:
- `valence` → `mood / 10.0` (computed, read-only)
- `arousal` → `attention / 10.0` (computed, read-only)
- `get_vad()` → `(valence, arousal, dominance)` tuple for Live2D
- `load_state(mood, attention, affection, dominance=0.5)` — backward compat
- `get_state()` — now includes `"dominance"` key
- `update(text, time_gap_hours, intent="statement")` — `intent` param added; `dominance` updated per turn based on intent + keywords + attention + affection
- `emotion_from_state()` — rewritten using VAD coordinates; uses `dominance` to distinguish `furious` vs `disappointed`, `cold` vs `sad`
- `describe_internal_state()` — includes dominance description

`core.py` changes:
- `emotion.update()` receives `intent=intent`
- `_original_dominance` saved/restored around viewer turns (same pattern as `_original_affection`)
- `conv_state.get_temperature()` receives `dominance` → low dominance → -0.05 temp, high → +0.05
- Return dict includes `"dominance"` and `"vad"` keys

#### Cognitive Appraisal Theory (Lazarus, 1991) ✅ Done

`_appraise(intent, has_positive, has_negative, text_len)` — pure function inside `EmotionEngine`, no LLM call, no new state.

Classifies each input on 2 axes:
- **Congruence**: `CONGRUENT` / `INCONGRUENT` / `IRRELEVANT` (vs Lyra's goals)
- **Control**: `HIGH` / `LOW` (can Lyra handle it?)

Returns `(mood_multiplier, dom_multiplier)` — scales existing mood/dominance deltas:

| Appraisal | mood_mult | dom_mult | Emotion |
|-----------|-----------|----------|---------|
| CONGRUENT + HIGH | 1.3 | 1.3 | Joy/Pride |
| CONGRUENT + LOW | 0.8 | 0.8 | Relief |
| INCONGRUENT + HIGH | 1.3 | 0.0 | Anger (dom delta cancelled) |
| INCONGRUENT + LOW | 1.5 | 1.8 | Anxiety/Guilt |
| IRRELEVANT + HIGH | 0.5 | 0.5 | Neutral |
| IRRELEVANT + LOW | 0.3 | 0.3 | Neutral + uncertain |

Control is LOW when: `text_len > 60` (complex question), `attention <= 2` (tired), or `affection < 25` (stranger).

**Key invariant**: appraisal *scales* existing keyword-based deltas — it does not replace them. Keyword matching ("gut reaction") still runs first.

**Known architecture limitation**: `emotion.update()` is called before viewer `affection` override in `chat()`, so `is_stranger` check uses owner affection for viewer turns. Not a crash — just means stranger detection doesn't fire for viewers.

#### Hydraulic Model (Lorenz) ✅ Done

`EmotionEngine` now has an emotional reservoir that accumulates irritability over time and triggers outbursts when overloaded.

New state:
- `irritability: float` — reservoir 0.0 → 1.0, session-level (resets on `load_state()`, not persisted)
- `_outburst_this_turn: bool` — per-turn flag, reset at start of each `update()` hydraulic block
- `OUTBURST_THRESHOLD: float = 0.85` — class constant

Accumulation logic (inside `update()`):
- 2+ negative keywords → `+0.20`
- 1 negative keyword → `+0.12`
- text < 5 chars (ignored) → `+0.05`
- positive keyword → `-0.15` (stress relief)
- neutral turn → `-0.04` (natural drain)

Outburst trigger runs **after** `smooth_transition()` so the mood spike is not dampened:
- `mood -= 4`, `dominance -= 0.2`, `irritability = 0.0`, `_outburst_this_turn = True`

`describe_internal_state()` injects:
- `irritability >= 0.6` → subtle warning ("patience wearing thin")
- `_outburst_this_turn` → `[EMOTIONAL OUTBURST]` strong hint

`core.py` return dict includes `"irritability"` key.

**Key invariants**:
- `irritability` is intentionally NOT save/restored for viewer turns — it's session-level state that accumulates across all turns
- Outburst + Predictive Surprise can theoretically conflict (both inject contradictory tone hints) — probability is very low (5% × outburst chance) and not currently guarded

#### Plutchik's Wheel — Secondary Emotions ✅ Done

`emotion_from_state()` now checks secondary emotions **before** primary emotions. 5 secondary emotions mapped to existing Live2D labels:

| Secondary | Plutchik | Conditions | Label |
|-----------|----------|------------|-------|
| Love | Joy + Trust | `0.3 <= v < 0.8`, affection >= 75 | `loving` |
| Contempt | Anger + Disgust | v <= -0.4, a 0.5-0.8, d >= 0.6, **irritability >= 0.4** | `furious` |
| Awe | Surprise + Fear | `0.2 <= v < 0.8`, a >= 0.7, d <= 0.4 | `thinking` |
| Remorse | Sadness + Disgust | -0.5 <= v <= -0.2, `0.3 < a <= 0.4`, d <= 0.35 | `sad` |
| Alarm | Fear + Surprise | `v <= -0.3`, a >= 0.7, d <= 0.35 | `disappointed` |

Note: Upper bounds on `v` for Love and Awe prevent hiding `ecstatic`. Remorse lower bound on `a` prevents conflict with `bored`. Alarm threshold raised to avoid triggering on mildly negative mood.

`Contempt` is the only secondary emotion that uses `self.irritability` — creating a cross-system dependency between Hydraulic Model and Plutchik. This is intentional: contempt requires sustained irritation, not just a single negative event.

#### Emotional Homeostasis (Hedonic Adaptation) 📋 Not Yet Implemented

Per-turn micro-decay toward baseline. Planned for `emotion.py`:
- `mood` decays toward 0 at rate 0.08/turn
- `dominance` decays toward 0.5 at rate 0.05/turn
- `irritability` drain already handled by Hydraulic Model (-0.04/turn)
- `affection` and `attention` excluded — they have their own decay logic

When implemented: add `_apply_homeostasis()` called at end of `update()`.

---

### 9. Conversation State Machine

File: `conversation_state.py` — `ConversationStateDetector`

States: `greeting → building → deepening → shifting → closing → goodbye`

Features:
* **Rhythm Detection**: Tracks avg user message length (rolling window 10 turns) → injects length hint into prompt via `get_rhythm_hint()`
* **Vibe Tier Tracking**: `_slang_count` / `_intellectual_count` updated every turn. Exposed via `get_vibe_tier()` → `"slang"` | `"intellectual"` | `"neutral"`
* **Dynamic Temperature** (`get_temperature()`): Maps emotion state + conversation state + vibe tier to LLM temperature (0.55–1.10)
  * closing/goodbye → 0.60
  * deepening → 0.75
  * bored/angry → +0.10
  * slang tier → +0.08
  * intellectual tier → -0.08
* **Pace Max Tokens** (`get_pace_max_tokens(base)`): Scales base token limit by user message pace. Scale-up gated by Lyra's energy level.
* **Reward Schedule** (`should_trigger_reward()`): 7% chance, cooldown 3 turns.
* **Ideology Trigger** (`should_trigger_ideology()`): 15% internal roll, cooldown 5 turns, no-repeat index tracking.
* **Surprise Trigger** (`should_trigger_surprise()`): 5% chance, cooldown 5 turns. Cross-cooldown with ideology.

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

| source_type | Who | Affection | Memory write policy |
|-------------|-----|-----------|---------------------|
| `owner` | Streamer via STT | From `memory.db` | ✅ Full (shared + creator-private) |
| `regular_viewer` | Known viewer | From `regular_viewers` table | ✅ Shared memory (selective) |
| `new_viewer` | Unknown viewer | Fixed at 10 | ✅ Shared memory (more selective) |
| `donor` | Super Chat sender | Boosted +20 temporarily | ✅ Shared memory (event-aware) |

**Critical rule (updated):**
* Memory is **shared across owner + stream** by default because stream is the main learning channel.
* Do **not** expose creator-private memory (owner profile details/diary internals) in viewer-facing prompts/replies.

---

### Priority Queue System

Messages from YouTube are processed in strict priority order:

```
Tier 0: owner        → Queue maxsize=10, processed instantly, bypasses cooldown
Tier 1: donor        → Queue maxsize=20, processed first always
Tier 2: regular_viewer → Queue maxsize=50
Tier 3: new_viewer   → Random pool maxsize=100, pick 1 every N seconds
```

Owner bypasses the cooldown entirely. STT from Web uses `/chat` directly, and comments from the Owner's YouTube account (configured via `OWNER_YOUTUBE_ID`) are processed as Tier 0 and `source_type="owner"`.

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
