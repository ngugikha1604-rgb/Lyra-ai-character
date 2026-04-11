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

> Build a **real-feeling AI companion**, not a task assistant.

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

## 🌐 Web Layer

* Frontend: `index.html`
* Backend: Flask server

Responsibilities:

* Send user input → MiniAI
* Return AI response
* (Optional) render emotion / UI

Note:

> Web layer is thin. All intelligence is in `core.py`.

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
