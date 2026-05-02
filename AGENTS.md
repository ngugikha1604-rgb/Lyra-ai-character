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
*   **Automatic Consolidation**: Stale L1 items are moved or deleted to keep the database clean.

---

### 2a. 🧬 Inspired Cognitive Logic (Lightweight Implementation)

Lyra incorporates high-level cognitive patterns from modern AI frameworks, implemented as native, lightweight Python logic to avoid library overhead:

*   **Mem0-inspired Persistence**:
    - **Logic**: Dynamic fact extraction and conflict resolution.
    - **Lyra Implementation**: Uses `_detect_conflict` in `memory.py` with vector embeddings (Cosine Similarity > 0.82) to identify if a new user fact contradicts an old one. Old facts are marked `superseded=1` and archived, while the core L1 memory stays "clean" and updated.
*   **LangGraph-inspired State Routing**:
    - **Logic**: Cyclic graph-like execution and conditional edge routing.
    - **Lyra Implementation**: The `chat()` method in `core.py` acts as a state controller. If the AI output contains `skill_needed`, the system "routes" the execution back to the model with new context (Skill Loop). The `ConversationStateDetector` manages transitions between 6 distinct states, acting as a lightweight state-persistence layer.
*   **Cognee-inspired Semantic Layering**:
    - **Logic**: Organizing unstructured data into hierarchical cognitive layers.
    - **Lyra Implementation**: Implemented via the **Three-Layer Memory (L1/L2/L3)**. Instead of a flat RAG, Lyra uses a specialized `MemoryRanker` (Reranker logic) to pick the most salient items across layers, mimicking how human cognition prioritizes facts vs. recent events vs. deep episodes.
*   **Hermes-inspired Reflection (Agentic Monologue)**:
    - **Logic**: Mandatory internal chain-of-thought and self-correction.
    - **Lyra Implementation**: Every response requires a `monologue` field. The **Thought Chaining** module (Section 11) takes this further by allowing the AI to "think twice" before replying, essentially an autonomous agentic reflection loop built directly into the conversation flow.
*   **Generative Agents-inspired Memory Stream & Planning**:
    - **Logic**: Importance-based memory scoring, periodic reflection loops, and dynamic objective planning (Park et al., 2023).
    - **Lyra Implementation**:
        1. **Importance Score**: New memories are batch-scored (1-10) by a light model. High-importance items boost their retrieval weight (`weight * (1 + saliency/10)`).
        2. **Reflection Loop**: Every 20 turns, Lyra triggers a background "reflection" task to synthesize 2-3 **Key Insights** (stored in `live_context.json`) about the current state of the session.
        3. **Dynamic Planning**: At stream start, Lyra generates a 3-5 item **Agenda**. This plan is auto-updated (marked as `done`) based on insights generated during the Reflection Loop.

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

* `mood` (-10 → +10) — Valence proxy
* `affection` (0 → 100) — Relationship depth, persisted to DB
* `attention` (0 → 10) — Arousal proxy
* `dominance` (0.0 → 1.0) — Confidence/control level *(new — VAD)*
* `irritability` (0.0 → 1.0) — Emotional reservoir, session-level *(new — Hydraulic)*

Effects:

* Changes tone of responses
* Influences behavior (playful vs calm)
* Persisted partially (affection only — dominance/irritability are session-level)
* **Fatigue System**: `attention` drains per chat (-0.3) and recovers during silence (+2.0/hr). Low attention triggers tired, short responses.
* **Emotion Decay**: Long periods of silence (>12h) decay `mood` by 50% towards 0 (neutral).
* **Affection Cap**: Caps relationship spikes at +/- 5 per turn for natural progression.
* **Homeostasis**: Per-turn micro-decay — `mood` → 0 at 8%/turn, `dominance` → 0.5 at 5%/turn.
* **Outburst**: When `irritability >= 0.85`, Lyra reacts with raw frustration (`mood -= 4`, `dominance -= 0.2`).

> See section 8b for full emotion architecture details.

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
* Summarizes old messages automatically (standard summarization)
* **Mega Summary Compression**: When standard summaries exceed 8 entries, a light model compresses them into a single "Mega Summary" (`is_mega=1`) to preserve multi-session context efficiently.
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

### 8c. 🧠 Behavioral Psychology Layer (PLAN.md — 5/5 Implemented ✅)

Five behavioral/linguistic systems layered on top of the emotion engine. All owner-only unless noted. Files: `core.py`, `conversation_state.py`, `prompts.py`, `vts_api.py`, `web.py`.

#### Module 1 — Variable Ratio Reinforcement (Skinner) ✅ Done

`should_trigger_reward()` in `conversation_state.py` — returns reward type string or `None`.

**5 reward types** with weighted selection:

| Type | Weight | Context guard | Effect |
|------|--------|--------------|--------|
| `deep_recall` | 40% | Rare memory must exist | Nhắc kỷ niệm hiếm |
| `healthy_debate` | 25% | Always available | Phản biện nhẹ |
| `vulnerability` | 15% | `state == "deepening"` only | Bộc lộ điểm yếu |
| `curiosity_spike` | 10% | `attention >= 4` | Hỏi ngược bất ngờ |
| `silent_approval` | 10% | `mood >= -2` | Im lặng tán thưởng |

**Behavioral shaping**: `_positive_behavior_streak` tracks engaged turns (long msg / question / intellectual). Each streak level adds +1.5% to base 7% probability, capped at 22%.

**Cooldown split**: `_last_reward_attempt_turn` (set on roll) vs `_last_reward_turn` (set only on deliver via `confirm_reward_delivered()`). Skipped rewards don't consume the deliver cooldown.

**Key invariant**: `reward_hint` blocks ideology, surprise, AND self-disclosure — set before all three in `chat()`.

#### Module 2 — Speech Act Classifier (Austin/Searle) ✅ Done

`classify_illocution(text, intent)` in `core.py` — returns `(illocution_type, perlocution_hint)`.

**5 illocution types** (heuristic, no LLM call):

| Type | Signals | Perlocution directive |
|------|---------|----------------------|
| `expressive` | Emotion words, ending particles | Empathy first, no advice |
| `directive` | Questions, requests | Direct and helpful |
| `commissive` | "mình sẽ", plans | Support and encourage |
| `assertive` | "xong rồi", achievements | Acknowledge naturally |
| `declarative` | "thôi kệ", conclusions | Brief acknowledgment |

**Check order**: expressive (keyword) → commissive → expressive (ending fallback) → assertive → declarative → directive (from intent) → neutral.

`perlocution_hint` injected into `build_prompt()` before reward_hint. Owner-only. `illocution` key added to `chat()` return dict.

#### Module 3 — LSM Tracker (Giles/Pennebaker) ✅ Done

`get_lsm_directive(dominance)` in `conversation_state.py` — returns convergence/divergence directive.

**New state**: `_expressiveness_score` (0.0 → 10.0) — tracks emoji, `!`, ALL CAPS, expressive words. Natural drain -0.5/turn.

**3 directive types**:
- `[LSM — EXPRESSIVE]`: score >= 6.0 AND not diverging → mirror user's energy
- `[LSM — FLAT]`: score <= 1.0 AND turn >= 8 AND tier not in (slang, intellectual) → tone down
- `[LSM — DIVERGE]`: raw score >= 8 AND state in (deepening, shifting) AND dominance >= 0.65 → maintain own voice

**Guard**: EXPRESSIVE and DIVERGE are mutually exclusive (DIVERGE wins). FLAT blocked when tier == slang (conflict with VIBE SYNC). LSM only injected for owner chat.

#### Module 4 — Self-Disclosure Engine (Walther) ✅ Done

`_get_self_disclosure_hint(intent, illocution)` in `core.py` — returns hint string or `""`.

**Guards**: `affection >= 50`, `irritability < 0.4`, cooldown 8 turns, 12% base probability.

**4 disclosure types** selected by context:

| Type | Trigger condition |
|------|------------------|
| `processing_state` | `illocution == "directive"` + `intent == "question"` |
| `uncertainty` | `dominance <= 0.35` |
| `aesthetic_reaction` | `illocution == "assertive"` + `affection >= 60` |
| `preference` | `affection >= 65` + `illocution in (expressive, assertive, commissive)` |

**Conflict guards** (in `chat()` after reward block):
- `reward_hint` set → clear `_self_disclosure_hint`
- `active_inference_mode` set → clear `_self_disclosure_hint`

`_last_disclosure_turn` initialized in `MiniAI.__init__()`.

**Module 2 → Module 4 connection**: `_illocution_type` from `classify_illocution()` is passed directly to `_get_self_disclosure_hint()` as input signal.

#### Module 5 — Paralinguistics / Live2D (text-side) ✅ Done

**VTube Studio (VTS) Bridge** (`vts_api.py`):
Kết nối WebSocket tới VTS (mặc định port 8001). Cần bật "Start API" trong VTS và lưu `vts_token.json`.

**VAD → Live2D Parameter Mapper**:
`update_vad_params(valence, arousal, dominance)` — maps 3D emotion space to Live2D params:

| VAD | Live2D param | Range |
|-----|-------------|-------|
| valence × 0.4 | `ParamBrowLY`, `ParamBrowRY` | [-0.4, +0.4] |
| 0.4 + arousal × 0.6 | `ParamEyeLOpen`, `ParamEyeROpen` | [0.4, 1.0] |
| (dominance - 0.5) × 10 | `ParamBodyAngleX` | [-5, +5] |

**Hotkey Mapping (Emotions)**:
Các hotkey cần setup trong VTS với ID tương ứng:
- `neutral` → `RESET`
- `happy` → `EXP_HAPPY`, `ecstatic` → `EXP_HAPPY_MAX`
- `sad` → `EXP_SAD`, `disappointed` → `EXP_SAD_MIN`
- `angry` → `EXP_ANGRY`, `furious` → `EXP_ANGRY_MAX`
- `thinking` → `EXP_THINKING` (kích hoạt ngay khi LLM bắt đầu xử lý)
- Other: `bored`, `friendly`, `loving`, `sleeping`, `cold`, `observing`.

**Action Mapping**:
LLM action (ví dụ `wave`, `nod`) → `ACT_<ACTION_NAME>` (ví dụ `ACT_WAVE`).

**Lip Sync (Khuyên dùng)**:
TTS Audio (VB-Cable ID 15) → VTS Microphone Lip-Sync (chọn input là VB-Cable). Không cần code.

**Idle Behavior**:
Tự động kích hoạt sau 5 giây không hoạt động. Avatar sẽ lắc lư nhẹ (swaying) qua `ParamBodyAngleX` / `ParamAngleZ` và chớp mắt ngẫu nhiên (3% mỗi 0.1s).

**Action Interruption**:
`clear_audio_queue()` trong `web.py` ngắt âm thanh và queue hiện tại khi có tin nhắn mới đè lên.

**Prosody Speed Mapping** (`web.py` `/speak` route):
Maps `lyra_ai.emotion.attention` → FPT TTS `speed` header:
- attention <= 2 → `"-2"` (rất chậm)
- attention <= 4 → `"-1"` (chậm)
- attention 5-7 → `"0"` (bình thường)
- attention >= 8 → `"1"` (nhanh)

**Deferred** (needs Live2D model): micro-jitters, SSML break tags, breathing animation.

---

### 8d. 🧠 Autonomous Self-Evolving System (RLHF) ✅ Implemented

A lightweight reinforcement learning mechanism that allows Lyra to "evolve" her speaking style based on real-time audience feedback. Files: `rl_feedback_loop.py`, `core.py`, `web.py`, `memory.py`.

#### 1. Stream Feedback Loop (RL Tracker)
Every time Lyra speaks on stream, a **15-second Reward Window** is opened.
- All viewer messages during this window are captured as the "Environment's Reaction".
- Observations are buffered in `rl_feedback_buffer.json`.

#### 2. Reward Evaluation (The Evaluator)
Once the window closes, the interaction is scored using the `LIGHT_MODEL` (`qwen2.5:0.5b`).
- **Reward Metrics**: Sentiment (haha/khen/đỉnh), Engagement (chat velocity spikes), and Mirroring (viewers using Lyra's slang).
- **Score Range**: -10.0 to +10.0.
- **Guardrails**: Toxic chat or spam results in negative rewards and are filtered out.

#### 3. Pattern Promotion (The Review Node)
Triggered via `consolidate_post_stream()` during the `/stream/stop` sequence (CLS phase).
- **Success Patterns**: Interactions with `reward >= 7.0` are promoted to Pinecone (L3 Memory) with `kind="rl_few_shot"`.
- **Evolved Persona**: The top successful "vibes" of the day are distilled into a short instruction (e.g., *"Use more Southern slang to tease viewers"*) and saved to `live_context.json` constraints.

#### 4. Retrieval & Reinforcement
During future streams, the memory system retrieves these patterns:
- **Weight Boost**: `rl_few_shot` items receive a **x4.0 weight boost** in the ranking module.
- **Few-Shot Injection**: Successfully proven responses appear as `[Mẫu thành công]` hints in Lyra's prompt, encouraging her to repeat high-engagement behaviors.

---

### Behavioral System Priority Chain (CRITICAL)

All behavioral directives in `chat()` follow strict priority — never 2 active at once:

```
reward_hint → blocks ideology, surprise, self_disclosure
active_inference_mode → blocks self_disclosure
```

Injection order in `build_prompt()`:
```
perlocution_hint → self_disclosure_hint → reward_hint → surprise_hint
```

Never add new behavioral directives without respecting this chain.

### 8b. 🧬 Emotion Architecture Upgrade (PLAN.md — 5/5 Implemented ✅)

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

#### Emotional Homeostasis (Hedonic Adaptation) ✅ Done

Per-turn micro-decay toward baseline. Prevents Lyra from getting "stuck" in an emotional state.

- `MOOD_DECAY_RATE = 0.08`, `DOMINANCE_DECAY_RATE = 0.05` — class constants
- `_apply_homeostasis()` — called at end of `update()`, **after** outburst trigger
- `mood` decays toward 0 at 8%/turn (~12 turns to halve from peak)
- `dominance` decays toward 0.5 at 5%/turn (~10 turns to halve gap from baseline)
- `irritability` excluded — Hydraulic already drains it (-0.04/turn)
- `affection` and `attention` excluded — have their own decay logic
- `previous_mood` is synced to `mood` after decay — prevents `smooth_transition()` from "bouncing back" mood toward the pre-homeostasis value on the next turn

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
* **Reward Schedule** (`should_trigger_reward()`): Returns reward type string or `None`. Base 7% + behavioral shaping. Cooldown split: attempt vs deliver. 5 reward types with weighted selection.
* **Behavioral Shaping** (`_positive_behavior_streak`): Tracks engaged turns. Each streak level +1.5% to reward probability.
* **Expressiveness Tracking** (`_expressiveness_score`): 0.0 → 10.0. Tracks emoji, `!`, ALL CAPS. Used by LSM Tracker.
* **LSM Directive** (`get_lsm_directive(dominance)`): Returns EXPRESSIVE / FLAT / DIVERGE directive. Owner-only.
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

### 10a. 🔎 Heuristic Web Search (RAG) ✅ Implemented

Lyra can proactively search the web to answer factual questions or get latest news. File: `model_utilities.py`.

- **Heuristic Trigger**: `_should_search(user_input)` uses regex to detect question patterns (what/who/where/when/how) and keywords (tin tức, thời tiết, giá...).
- **Privacy Guard**: Search is disabled if the input contains personal pronouns (I, me, my, tôi, mình) to avoid searching private info.
- **Provider**: Uses `duckduckgo_search` (DDGS) for anonymous, tracker-free results.
- **Injection**: Top 3 search results are formatted into a concise snippet and injected into `search_context` in the prompt.
- **Constraint**: Only triggered if `SEARCH_ENABLED=True` in `config.py`.

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

### 13. 🛠️ Skill System & Autodidactic Loop ✅ Implemented

A meta-learning framework that allows Lyra to discover and refine "Skills" over time. Files: `skill_synthesizer.py`, `core.py`, `prompt_builder.py`.

#### 1. Skill Synthesis (The Teacher Node)
Triggered every **25 turns** during stream or private chat.
- **Input**: The last 10 turns of conversation.
- **Logic**: A light model (`SkillSynthesizer`) analyzes the interaction to detect unique behavior patterns or complex problem-solving methods demonstrated by Lyra.
- **Output**: If a new skill is found, it generates a markdown file (e.g., `skills/sarcastic_teasing.md`) with instructions and examples.

#### 2. Skill Indexing & Stats
- All learned skills are registered in `skills/_index.md`.
- `skill_stats.json` tracks `call_count` and `last_used` for each skill.
- **Stale Removal**: Skills not used for 30 days and with <3 calls are automatically deleted ("forgotten") to keep the library clean.

#### 3. Dynamic Execution
- During standard chat, if Lyra's monologue indicates she needs help, she can set `skill_needed` in her JSON response.
- **The Loop**: `core.py` detects the field → loads the requested `.md` file → re-calls the model with the skill's specific instructions.
- This allows Lyra to "level up" her complexity on demand without bloating the base prompt.

---

### 14. 📣 Proactive Stream Monitoring (Silence Fill) ✅ Implemented

A background thread in `web.py` ensures the stream never goes silent.

- **Threshold**: 120 seconds of chat inactivity.
- **Logic**: `_proactive_monitor` thread checks the gap since the last viewer message.
- **Context Injection**: Loads the `current_focus` from `live_context.json` (e.g., "Genshin gameplay").
- **Generation**: A light model generates a 1-sentence thought or question to re-engage the audience.
- **SSE Broadcast**: The proactive message is sent directly to the frontend via SSE as a `proactive_question` type.

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
3. Backend maps `lyra_ai.emotion.attention` → FPT speed string (`"-2"` to `"1"`)
4. Backend calls FPT AI TTS API (`voice: banmai`, dynamic speed)
5. FPT returns async URL → backend fetches MP3 → streams to frontend
6. Frontend plays audio with lip sync via Web Audio API analyser
7. After each chat reply, `vts_bridge.update_vad_params(v, a, d)` updates Live2D params continuously

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

---

### 15. 🧠 Generative Agents Cognitive Upgrade (Feature Set) ✅ Implemented

Bộ tính năng nâng cấp khả năng nhận thức và lập kế hoạch dựa trên nghiên cứu **Generative Agents (Park et al., 2023)**.

#### A. Memory Stream with Importance Score
- **Mục tiêu**: Lyra ưu tiên những thông tin quan trọng thay vì nhớ máy móc.
- **Triển khai**: 
    - Khi buffer bộ nhớ được flush, hệ thống gọi `_llm_importance_score` (Batch call) để chấm điểm 1-10 cho tất cả items.
    - Điểm này lưu vào cột `saliency` trong DB.
    - `MemoryRanker` sử dụng công thức `weight * (1 + saliency/10)` để đẩy các ký ức quan trọng lên đầu context window.
- **Tệp liên quan**: `memory.py`, `memory_handler.py`.

#### B. Reflection Loop (Vòng lặp Suy ngẫm)
- **Mục tiêu**: Tổng hợp bối cảnh hội thoại thành các "Thấu hiểu" (Insights) cấp cao.
- **Triển khai**:
    - Trigger mỗi **20 tin nhắn** (`REFLECTION_INTERVAL`).
    - Phân tích 20 messages gần nhất + emotion state + session items.
    - Sinh ra 2-3 insights (ví dụ: "User đang stress vì công việc", "Viewer thích nghe Lyra hát").
    - Lưu vào `live_context.json` (TTL 15m) và inject vào prompt qua block `[INSIGHTS]`.
- **Tệp liên quan**: `core.py`, `live_context.py`.

#### C. Dynamic Planning (Kế hoạch Động)
- **Mục tiêu**: Tạo Agenda cho stream và tự động cập nhật tiến độ.
- **Triển khai**:
    - **Generate**: Khi stream start, Lyra gen 3-5 mục tiêu cụ thể (Agenda).
    - **Update**: Sau mỗi Reflection Loop, hệ thống dùng Insights mới để đối soát và đánh dấu `done` cho các mục tiêu đã đạt được.
    - **Visibility**: Chỉ hiển thị các mục tiêu `pending` trong prompt qua block `[STREAM PLAN]`.
- **Tệp liên quan**: `stream_handler.py`, `web.py`, `core.py`.

## 📌 Summary for AI Agents

If you are an AI working on this project:

* You are not building a chatbot
* You are maintaining a **character with memory and emotion**
* Every change must preserve:

  * personality
  * natural conversation
  * lightweight performance

---
