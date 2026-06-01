# AGENTS.md — Lyra AI Technical Notes

> Tài liệu này dành cho agent/developer làm việc trực tiếp với codebase. Nội dung kiến trúc tổng quan, cấu trúc thư mục, và hướng dẫn setup nằm ở **README.md**.

---

## 🧭 Design Decisions (Tại sao lại làm vậy?)

### Tại sao dùng SQLite + Pinecone thay vì một DB duy nhất?

SQLite (L1/L2) và Pinecone (L3) phục vụ hai mục đích hoàn toàn khác nhau:
- **SQLite** lưu *structured facts* — tên, sở thích, lịch sử hội thoại. Query bằng SQL, không cần embedding, latency thấp.
- **Pinecone** lưu *episodic memories* dưới dạng vector — dùng cho semantic search (RAG) khi cần tìm ký ức liên quan theo ngữ nghĩa, không phải theo keyword.

Dùng Pinecone cho tất cả sẽ tốn tiền và latency cao cho mọi query. Dùng SQLite cho tất cả thì không thể làm semantic retrieval. Hybrid là lựa chọn tối ưu cho use case này.

### Tại sao dùng DSPy thay vì raw prompt?

Raw prompt strings có 3 vấn đề: (1) khó test, (2) output format không ổn định, (3) không thể optimize tự động. DSPy giải quyết cả 3:
- `LyraChatSignature` định nghĩa I/O contract rõ ràng — dễ validate, dễ mock trong test.
- `ChainOfThought` buộc model sinh `rationale` trước reply — giảm hallucination và tăng coherence.
- DSPy compiler có thể optimize few-shot examples tự động, lưu vào `lyra_compiled.json`.

Fallback về `parse_vbrain_response()` vẫn tồn tại để đảm bảo không bao giờ hard-fail khi DSPy unavailable.

### Tại sao Skills lấy ý tưởng từ Hermes / tool-use pattern?

Skills là Markdown files trong `/skills/` — không phải Python code. Lý do:
- LLM đọc được trực tiếp, không cần parse.
- Có thể sinh ra tại runtime bởi `SkillSynthesizer` mà không cần restart server.
- Dễ inspect, edit, và version control.
- `protected=True` trong frontmatter ngăn cleanup tự động — dùng cho skills quan trọng.

Pattern này tương tự Hermes function-calling nhưng không cần JSON schema — phù hợp hơn với conversational context.

### Tại sao dùng Mixin thay vì composition thông thường?

`MiniAI` cần truy cập `self.memory`, `self.emotion`, `self.conv_state` từ nhiều Mixin khác nhau. Nếu dùng composition (dependency injection), mỗi Mixin phải nhận references qua constructor — verbose và dễ circular dependency. Mixin cho phép tất cả share `self` mà không cần wiring thủ công.

Trade-off: MRO (Method Resolution Order) phức tạp hơn. Khi override method, luôn kiểm tra `super()` chain.

### Tại sao VAD 3D thay vì sentiment score đơn giản?

Sentiment score (positive/negative) không đủ để model hóa hành vi phức tạp:
- Một người có thể *vui* (valence cao) nhưng *mệt* (arousal thấp) — cần reply ngắn, nhẹ nhàng.
- Một người có thể *tức* (valence thấp) nhưng *tập trung* (arousal cao) — cần reply thẳng thắn, không né tránh.
- `dominance` quyết định Lyra có "nhường" hay "giữ lập trường" trong conflict.

VAD là model chuẩn trong affective computing (Russell, 1980). Hydraulic model (Lorenz) thêm vào `irritability` accumulation để tránh Lyra bị "bully" mà không phản ứng.

### Tại sao 9router thay vì gọi Groq trực tiếp?

9router là local reverse proxy cho phép:
- Swap provider mà không đổi code (chỉ đổi config trong 9router).
- Rate limit và retry logic tập trung.
- Log tất cả LLM calls ở một chỗ để debug.
- DSPy chỉ biết `openai/groq/<model>` — không cần biết API key hay base URL thật.

---

## 🔒 Invariants (Không được vi phạm)

### Persona
- Lyra **luôn** xưng "em", gọi owner là "anh". Không bao giờ xưng "tôi" hay gọi user bằng tên lạ.
- Đây là constraint ở tầng persona prompt — không phải hardcode trong code. Nhưng nếu thay đổi prompt, phải giữ nguyên rule này.

### `MiniAI.chat()` là single source of truth
- `reward_hint` và `active_inference_mode` chỉ được quyết định trong `MiniAI.chat()` — không di chuyển logic này sang bất kỳ Mixin nào.
- `_skill_loop_count` và `_vts_loop_count` **phải** được reset về `0` ở đầu mỗi lượt `chat()`. Nếu quên, loop sẽ bị skip ngay từ lượt đầu sau lần đầu tiên dùng skill/VTS.

### Background tasks
- **Không** tạo `threading.Thread`, `threading.Timer`, hoặc `ThreadPoolExecutor` mới ngoài `core.py`.
- Tất cả async I/O phải đi qua `enqueue(PRIORITY, func)` từ `background_worker.py`.
- Vi phạm rule này sẽ tạo ra unbounded threads và memory leak trong long-running stream sessions.

### Database
- Mọi SQLite write phải qua `DB_LOCK` từ `memory.py`.
- Không query `get_analytics()` hay `get_history()` ngoài `with DB_LOCK:`.
- DB chạy WAL mode — file `.db-wal` và `.db-shm` là bình thường, không xóa.

### Stream context tier
- `_stream_ctx` phải ở **TIER 1** trong `PromptBuilderMixin` (cùng với `base_personality`).
- Nếu để ở TIER 2 hoặc thấp hơn, stream context sẽ bị drop khi memory context lớn — Lyra sẽ không biết đang stream.

### YouTube auto-detect
- **Không xóa** `get_current_live_stream_info` hay `get_live_chat_id` khỏi `youtube_chat.py`.
- **Không thêm** input field Video ID vào frontend.
- **Không thay đổi** flow auto-detect ở `/stream/start`.
- User không cần nhập ID thủ công — đây là thiết kế cố ý.
- `google-api-python-client` phải giữ trong requirements.

---

## ⚠️ Gotchas & Pitfalls

### DSPy field thêm mới
Khi thêm output field mới vào `LyraChatSignature`, phải cập nhật **cả hai**:
1. `LyraChatSignature` trong `dspy_modules/signatures.py`
2. `_parse_brain_result()` trong `core.py` (hoặc nơi parse output)

Nếu chỉ thêm vào signature mà không parse, field sẽ bị bỏ qua silently.

### `_maybe_broadcast_mood_stage()` và deadlock
Method này **phải** được gọi **ngoài** `with self._ai_lock` block. Nếu gọi bên trong lock, sẽ deadlock vì SSE broadcast cũng cần acquire lock trong một số code path.

### `_inject_arrival_hint()` — chỉ gọi `get_viewer_profile` 1 lần
Trước đây function này gọi `get_viewer_profile` 2 lần. Đã fix: gọi 1 lần, dùng lại kết quả cho cả callback lẫn check extract. Không refactor lại theo pattern cũ.

### `_personality_cache` không expire
`ViewerTracker._personality_cache` không tự expire trong session — đây là thiết kế cố ý (personality type ổn định trong 1 stream). Nếu cần reset giữa stream, gọi `viewer_tracker._personality_cache.clear()` thủ công.

### `_spy_observed` phải reset khi stream restart
`_spy_observed: set[str]` trong `StreamService` phải được reset trong `reset_greeted_set()` khi stream restart. Đã implement — đừng xóa dòng đó khi refactor.

### Groq không được gọi trực tiếp
Không gọi Groq API trực tiếp cho chat. Chỉ DSPy Brain qua 9router. Nếu 9router down, fallback về Ollama local (`_call_model()`), không fallback về Groq trực tiếp.

### `live_context.json` là ephemeral
`live_context.json` chỉ dùng cho stream session data (donations, focus, insights, plan). Không dùng để lưu long-term facts về user — đó là việc của SQLite L1.

### Conversation state gating
Không trigger `ideology` hoặc `surprise` khi:
- State là `closing` hoặc `goodbye`
- User mood là `sad`, `stressed`, hoặc `anxious`

Vi phạm rule này sẽ làm Lyra "vô duyên" — đẩy philosophical question khi user đang buồn.

### Multi-model routing
- Primary replies → `_call_model()` (Ollama local, `CHAT_MODEL`)
- Memory extraction / summarize → `_call_light_model(provider="ollama")` (`qwen2.5:0.5b`)
- Reflection / planning → `_call_light_model(provider="gemini")` hoặc `"openrouter"`
- DSPy Brain → qua 9router tự động

Không hardcode API keys hay base URLs — luôn đọc từ `config.py`.

---

## 🛠️ Operational Guidelines

### Khi thêm route mới
- Đăng ký blueprint trong `app/__init__.py`.
- Inject dependencies qua `current_app` hoặc `app.*` attributes — không import `lyra_ai` trực tiếp vào route file.
- Áp dụng rate limit phù hợp qua `flask-limiter`.

### Khi thêm background task mới
```python
from background_worker import enqueue, PRIORITY_NORMAL
enqueue(PRIORITY_NORMAL, my_function, arg1, arg2)
```
Chọn priority dựa trên impact: owner data = CRITICAL, stream events = HIGH, housekeeping = NORMAL.

### Khi optimize DSPy brain
1. Chạy DSPy compiler với dataset examples.
2. Lưu output vào `lyra_compiled.json`.
3. Restart server — `LyraBrain` tự load từ file này nếu tồn tại.

### Khi thêm SSE event type mới
Cập nhật frontend handler để xử lý event type mới. Các event types hiện có: `emotion`, `action`, `reply`, `vts_state`, `mood_stage`, `shoutout`.

### Khi thêm skill mới thủ công
Tạo file `.md` trong `/skills/` với frontmatter:
```yaml
---
name: skill_name
description: Mô tả ngắn
protected: true  # nếu không muốn bị cleanup tự động
---
```

---

## 📋 Changelog

### Phase 2 — Engagement Foundation

#### 2.1 Stream Routes (`app/routes/stream.py`)
- **`POST /stream/stop`** — 5 bước đúng thứ tự: `yt_poller.stop()` → farewell broadcast (background) → `stop_promote_timer()` → `is_streaming = False` → `reset_live_context()`
- **`GET /stream/status`** — trả `load_live_context()` + `is_streaming`, không gọi DB
- **`GET /stream/analytics`** — top viewers + queue snapshot
- **`GET /stream/debug/queue`** — queue snapshot thuần
- **`POST /stream-chat`** — nhận `{message, sender_id, sender_name}`, gọi `enqueue_event()`, không có logic riêng
- **`GET /viewers`** — top viewers với param `?limit=` (max 100)

#### 2.2 Running Bit Injection (`app/routes/stream.py`)
Trong `/stream/start`, sau `update_field("stream_start_time", ...)`: chọn ngẫu nhiên 1 bit từ `RUNNING_BITS` và inject vào `update_plan()`. `get_live_context_block()` tự render thành `[STREAM PLAN] □ goal`.

#### 2.3 Personality Type Cache (`viewer_tracker.py`)
- Thêm `_personality_cache: dict[str, str | None]` vào `ViewerTracker.__init__`
- Key format: `f"{platform}:{channel_id}:{sender_id}"`
- `get_stream_context()` chỉ gọi `get_viewer_recent_messages()` (DB read) lần đầu tiên, sau đó dùng cache
- Cache không expire trong session — personality type ổn định trong 1 stream
- Loại bỏ ~1000 DB reads không cần thiết trong stream 50 người × 20 tin

#### 2.4 Stream Mood Stage (`app/services/stream_service.py`)
- Thêm `_MOOD_STAGE_TEMPLATES` (class-level dict) với 2 stage: `hype` và `grumpy`
- Thêm `_last_broadcast_mood: float = 0.0` vào `__init__`
- Method `_maybe_broadcast_mood_stage()`: broadcast SSE event `"mood_stage"` khi `|mood - prev| >= 2.0`
  - `mood > 6` → stage `hype`
  - `mood < -3` → stage `grumpy`
  - neutral → im lặng
- Gọi ở 2 chỗ: sau silence decay trong `_consumer_loop`, sau mood boost từ velocity cao trong `_handle_event`
- ⚠️ Phải gọi **ngoài** `with self._ai_lock` — xem mục Gotchas

#### 2.5 Highlight Callback (`app/services/proactive_service.py`)
Trong `_choose_silence_line()`, thêm nhánh đầu tiên (trước roll < 0.60): 35% chance nhắc lại 1 highlight từ `live_context["stream_highlights"]`. Không cần LLM.

---

### Phase 3 — Smart Personality Layer

#### 3.1 RL Mid-Stream Feedback (`rl_feedback_loop.py`)
Trong `_evaluate_observation()`, sau `synthesize_from_rl` enqueue: khi `reward_score >= 8.0`, inject insight vào `live_context["current_insights"]` (giữ tối đa 3 cái gần nhất). 0 LLM call thêm. Wrapped trong try/except riêng — RL flow không bị gián đoạn nếu live_context lỗi.

#### 3.2 Spy Observation (`app/services/stream_service.py`)
- Thêm `_spy_observed: set[str] = set()` vào `__init__` (reset trong `reset_greeted_set()` khi stream restart)
- Trong `_handle_event()`, khi `tier == "regular_viewer"` và `message_count == 15` và `sender_id not in _spy_observed`: lấy 3 tin nhắn gần nhất, inject `[OBS]` hint vào `stream_ctx`
- Cost: 1 DB read per viewer per session tại mốc 15 tin
- Error handling: nếu `get_viewer_recent_messages` fail thì log và bỏ qua, không crash `_handle_event`

---

### Engagement Improvements (Zero CPU Overhead)

#### Memory Callback (`app/services/stream_service.py`)
Trong `_inject_arrival_hint()`: nếu viewer quen có `viewer_profile.notes`, inject 1 dòng hint để Lyra nhắc chi tiết cụ thể từ lần trước. `get_viewer_profile` giờ chỉ gọi 1 lần — kết quả dùng lại cho cả callback lẫn check extract.

#### Top Chatter Shoutout (`app/services/stream_service.py`)
Trong `_promote_loop()`: mỗi 30 phút, sau khi promote viewers, tự động broadcast SSE event `"shoutout"` với tên top 1-2 chatter. Dùng `get_top_viewers()` vốn đã chạy trong cùng loop. Template lines random, guard cho trường hợp chỉ có 1 viewer trong top. Wrapped trong try/except riêng.

#### Mở rộng BANGQUA Templates (`prompts.py`)
`STREAM_BANGQUA_TEMPLATES` từ 4 lên 14 câu, chia 4 nhóm:
- Silence observations (4 câu gốc)
- Inner monologue — tạo cảm giác AI có inner life (5 câu mới)
- Self-aware humor (3 câu mới)
- Teaser/cliffhanger nhẹ (2 câu mới)

---

*Created by Antigravity — Standardized for Lyra AI character development.*
