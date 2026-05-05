# AGENTS.md — Lyra AI Project (Architectural Specification)

## 🧠 Project Overview
Lyra is a high-fidelity conversational AI system designed to be a "real-feeling" companion. She is characterized as a 16-year-old younger sister with a distinct Vietnamese persona, emotional depth, and a multi-layered memory system.

**Core Goal**: Evolve into a fully autonomous VTuber persona capable of long-term relationship building and adaptive stream performance.

---

## 🏗️ Core Architecture (Mixin-Based)
The system uses a **Mixin-based Orchestrator** in `core.py` to manage complexity while keeping the `MiniAI` class manageable.

- **`MiniAI(ModelUtilityMixin, BehavioralLogicMixin, PromptBuilderMixin, StreamHandlerMixin, MemoryHandlerMixin)`**
- **Entry Point**: `chat(user_input, source_type="owner", ...)` is the single entry point.
- **Agentic Logic**:
    - **Skill Loop**: If the LLM requests a `skill_needed`, the system executes the skill and feeds the result back to the LLM (Master-Subordinate loop).
    - **VTS Feedback Loop**: When performing VTube Studio actions, the system captures runtime errors (e.g., disconnected API) and feeds them back to the LLM as "Internal Sensory Feedback" for self-correction.

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
2. **AI Extraction**: LLM-based structured trancription of memories.
3. **Conflict Resolution**: New facts are compared against L1. Contradictions mark old facts as `superseded`.
4. **Consolidation (CLS)**: "Sleep-phase" distillation where high-reward episodic memories are promoted to L1 Semantic core.

---

## 🎭 Emotion Engine 2.0 (VAD & Hydraulic Models)
Lyra doesn't use simple sentiment scores. She operates in a **3D VAD Space**:

- **Valence (Mood)**: [-10, +10] - How "good" or "bad" she feels.
- **Arousal (Attention)**: [0, 10] - Energy level and focus.
- **Dominance**: [0.0, 1.0] - Level of confidence and control in the conversation.

### Advanced Mechanics:
- **Cognitive Appraisal**: Incoming messages are evaluated for *Congruence* and *Control* (Lazarus, 1991), scaling mood/dominance shifts.
- **Hydraulic Model (Lorenz)**: `irritability` accumulates during annoying interactions and triggers an `outburst` when it exceeds 0.85, causing a sudden drop in mood and dominance.
- **Homeostasis**: Mood naturally decays towards 0 (neutral) at 8%/turn.

---

## 🧠 Behavioral Psychology Layers
Five specialized modules layered on top of the emotion engine:

1. **Variable Ratio Reinforcement (Skinner)**: 7% base chance to trigger "Rewards" (Deep Recalls, Vulnerability, Curiosity Spikes) to keep engagement unpredictable.
2. **Speech Act Classifier (Austin/Searle)**: Classifies user intent (Expressive, Directive, Commissive, etc.) to adjust Lyra's "Perlocutionary" goal.
3. **LSM Tracker (Giles/Pennebaker)**: Tracks Linguistic Style Matching. Lyra may *converge* to mirror the user or *diverge* to assert dominance.
4. **Self-Disclosure Engine (Walther)**: Controlled release of Lyra's "internal" thoughts to deepen relationship trust (requires `affection >= 50`).
5. **Active Inference**:
    - **Ideological Proactivity**: 15% chance to push a specific world-view or philosophical question.
    - **Predictive Surprise**: 5% chance to subvert current emotional patterns (e.g., being warm when she should be angry).

---

## 🎙️ Communication & VTuber Integration
### VTube Studio (VTS) Bridge
Lyra controls her Live2D avatar via a dedicated WebSocket bridge (`vts_api.py`).
- **VAD Mapping**:
    - `ParamBrowLY/RY` = `valence * 0.4`
    - `ParamEyeL/ROpen` = `0.76 + arousal * 1.14`
    - `ParamBodyAngleX` = `(dominance - 0.5) * 10`
- **Idle Behavior**: Automatic swaying and random blinking after 5s of inactivity.
- **Thinking State**: Triggers `EXP_THINKING` expression immediately upon receiving user input.

### TTS & Paralinguistics
- **FPT AI Integration**: Converts text to speech with dynamic speed mapping.
- **Prosody Mapping**: `attention` level influences TTS speed ("-2" for tired, "1" for excited).
- **Pitch Shifting**: Post-processes audio with `octaves=0.22` to achieve a more youthful, "younger sister" timbre.
- **Audio Routing**: Outputs via `VB-Cable` to allow OBS capture for streaming.

---

## 📈 Self-Evolving System (RLHF & Skills)
- **RL Feedback Loop**: Opens a 15s "Reward Window" after Lyra speaks on stream. Captures viewer chat to score the response (-10 to +10).
- **Post-Stream Review**: High-reward patterns are promoted to Pinecone (`rl_few_shot`) and distilled into `live_context.json` constraints for future sessions.
- **Skill Synthesizer**: Periodically analyzes successful interactions to generate new, repeatable Markdown-based skills in `/skills/`.

---

## 🛠️ Operational Guidelines for Agents
1. **Invariants**:
    - Never xur xing ("I/Me") or call the user anything other than "Anh".
    - `MiniAI.chat()` is the only place where `reward_hint` and `active_inference` should be decided to maintain priority chain logic.
2. **Context Management**:
    - Use the **4-tier greedy token budget** in `PromptBuilderMixin`.
    - `live_context.json` is for ephemeral stream data (donations, focus). Do not use it for long-term facts.
3. **Background Tasks**:
    - Use `enqueue(PRIORITY, func)` from `background_worker.py` for all non-critical I/O (summaries, diary, RL scoring).
4. **State Machine**:
    - Respect the 6 conversation states: `greeting`, `normal`, `deepening`, `shifting`, `conflict`, `resolution`.

---
*Created by Antigravity — Standardized for Lyra AI character development.*
