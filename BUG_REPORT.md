# Bug Report: core.py Analysis

## 🔴 Critical Bugs

### 1. **Memory Buffer Flush Logic - Flushes Too Often on Low-Value Content**
**Severity:** HIGH
**Location:** `should_flush_memory_buffer()` (Line ~1111)
**Issue:** Current logic flushes every 6 turns regardless of buffer content quality

```python
def should_flush_memory_buffer(self, intent=None):
    if not self.memory_buffer:
        return False
    if len(self.memory_buffer) >= 6:  # ❌ Always flush at 6 items
        return True
    if any(item.get("saliency", 0) >= 7 for item in self.memory_buffer):
        return True
    return self.turn_counter % 6 == 0 and len(self.memory_buffer) >= 3  # ❌ Periodic flush every 6 turns
```

**Problem:** May call AI extraction too often when buffer contains mostly low-value/trivial content, wasting API calls and processing time.

**Fix:** Add minimum total saliency threshold for the entire buffer:
```python
def should_flush_memory_buffer(self, intent=None):
    if not self.memory_buffer:
        return False

    total_saliency = sum(item.get("saliency", 0) for item in self.memory_buffer)
    avg_saliency = total_saliency / len(self.memory_buffer) if self.memory_buffer else 0

    # Require minimum buffer size AND minimum average saliency
    if len(self.memory_buffer) >= 6 and avg_saliency >= 4:
        return True

    # High-priority item trigger
    if any(item.get("saliency", 0) >= 7 for item in self.memory_buffer):
        return True

    # Reduced periodic flush with quality check
    return self.turn_counter % 10 == 0 and len(self.memory_buffer) >= 4 and avg_saliency >= 3
```

---

### 2. **Embedding Performance - Synchronous Computation on Save**
**Severity:** MEDIUM-HIGH
**Location:** `add_memory_item()` (Line ~780-790)
**Issue:** Computes embeddings immediately when saving memory items

```python
# In add_memory_item()
if emb_blob is None:
    embedding = self._get_embedding(str(value))  # ❌ CPU-heavy synchronous computation
    if embedding is not None:
        emb_blob = sqlite3.Binary(embedding.astype(np.float32).tobytes())
```

**Problem:** Slows down memory operations on low-end hardware. Embedding computation happens during save, blocking the response.

**Fix:** Implement lazy loading - compute embeddings only when needed for retrieval:
```python
# In add_memory_item() - Store without embedding
if emb_blob is None:
    # Don't compute here - mark for lazy loading
    emb_blob = None  # Will be computed on first retrieval

# Add lazy computation in retrieval methods
def get_relevant_memories(self, query_embedding):
    # Compute missing embeddings here
    for item in items:
        if item['embedding'] is None:
            item['embedding'] = self._get_embedding(item['value'])
            # Cache the result
```

---

### 3. **Proactive Message Race Condition - Potential Double-Send**
**Severity:** HIGH
**Location:** `get_proactive_message()` + web.py/discord_bot.py
**Issue:** No protection against concurrent proactive message sending

```python
# In web.py proactive() route
msg = lyra_ai.get_proactive_message()
if msg:
    # ❌ No check if user just messaged
    # Send proactive message immediately
    lyra_ai.memory["time_tracking"]["last_message_time"] = datetime.now().isoformat()
```

**Problem:** If user sends message milliseconds before proactive check runs, both messages could be sent simultaneously.

**Fix:** Add race condition protection:
```python
def get_proactive_message(self):
    # Check if recent activity occurred
    now = self.get_vietnam_time()
    last_time = self.memory.get("time_tracking", {}).get("last_message_time")
    if last_time:
        last_dt = datetime.fromisoformat(last_time)
        if (now - last_dt).total_seconds() < 30:  # Within 30 seconds
            return None  # Skip proactive message

    # Add sending flag to prevent concurrent sends
    if getattr(self, '_sending_proactive', False):
        return None
    self._sending_proactive = True

    try:
        # ... existing proactive logic ...
        return message
    finally:
        self._sending_proactive = False
```

---

### 4. **Memory Consolidation Aggressiveness - May Delete Important Rarely-Accessed Memories**
**Severity:** MEDIUM
**Location:** Memory forgetting logic (Line ~1750)
**Issue:** Only uses saliency score for deletion decisions

```sql
DELETE FROM memory_items
WHERE access_count = 0
AND (? - source_turn) > 100
AND saliency < 7  -- ❌ Only saliency-based deletion
```

**Problem:** Important but rarely-accessed memories (like long-term goals, core personality traits) may be deleted if they have low saliency but high importance.

**Fix:** Add importance field with slower decay:
```sql
-- Add importance column to memory_items table
ALTER TABLE memory_items ADD COLUMN importance REAL DEFAULT 1.0;

-- Update forgetting logic
DELETE FROM memory_items
WHERE access_count = 0
AND (? - source_turn) > 100
AND saliency < 7
AND importance < 1.5  -- Higher threshold for important memories
```

Update memory creation to set importance:
```python
def add_memory_item(self, kind, value, weight=1.0, limit=12):
    # Set importance based on memory type
    importance = {
        'goal': 2.0,      # Long-term goals decay slowly
        'like': 1.8,      # Core preferences
        'dislike': 1.8,   # Core preferences
        'relational': 1.5, # Important relationships
        'topic': 1.2,     # Topics decay faster
        'episodic': 1.0   # Recent events decay fastest
    }.get(kind, 1.0)

    # ... existing code ...
    c.execute(
        "INSERT OR IGNORE INTO memory_items (kind,value,weight,saliency,importance,...) "
        "VALUES (?,?,?,?,?,...)",
        (kind, str(value), weight, saliency, importance, ...)
    )
```

---

## 🟠 High-Priority Bugs

### 5. **Thread Safety Issue in Memory Access**
**Severity:** MEDIUM-HIGH
**Location:** `_get_db()` (Line ~500-510)
**Issue:** DB connection reuse without proper lock management

```python
def _get_db(self):
    """Connection reuse without full lock protection"""
    if self._db_connection is not None:
        try:
            self._db_connection.execute("SELECT 1")
            return self._db_connection
        except sqlite3.Error:
            self._db_connection = None

    conn = sqlite3.connect(DB_PATH, check_same_thread=False)  # ❌ Risky
    self._db_connection = conn
    return conn
```

**Problem:** `check_same_thread=False` allows unfettered access, connection not protected during initialization.

**Fix:** Use proper connection management with locks.

---

## 🟡 Medium-Priority Issues

### 6. **Cache Key Logic Error in `get_summary_context()`**
**Severity:** MEDIUM
**Location:** `get_summary_context()` (Line ~1550)
**Issue:** Cache key based only on user input

```python
def get_summary_context(self, user_input=""):
    cache_key = user_input[:50] if user_input else ""

    if self._summary_context_cache is not None and self._summary_context_cache.get("key") == cache_key:
        return self._summary_context_cache.get("value", "")
```

**Problem:** Same input across turns returns wrong cached summary.

**Fix:** Include turn counter in cache key.

---

## 🟢 Low-Priority Issues

### 7. **Unbounded Memory Buffer Growth**
**Severity:** LOW-MEDIUM
**Location:** `buffer_memory_candidate()` (Line ~1100-1130)
**Issue:** Buffer could grow if flush fails

```python
def buffer_memory_candidate(self, kind, value, saliency=None):
    self.memory_buffer.append({...})

    if len(self.memory_buffer) > 24:
        self.memory_buffer = self.memory_buffer[-24:]
```

**Problem:** If `flush_memory_buffer()` fails, buffer accumulates.

---

## 📋 Summary

| Severity | Count | Issues |
|----------|-------|--------|
| 🔴 Critical | 1 | Memory buffer flush logic |
| 🔴 High | 2 | Embedding performance, Proactive race condition |
| 🟠 Medium-High | 2 | Memory consolidation, Thread safety |
| 🟡 Medium | 2 | Cache logic, Buffer growth |
| 🟢 Low | 1 | Minor issues |

## ✅ Recommended Fixes (Priority Order)

1. **Fix memory buffer flush logic** → Reduce unnecessary AI calls
2. **Implement lazy embedding computation** → Improve performance
3. **Add proactive message race protection** → Prevent double-sends
4. **Add importance field to memory system** → Preserve valuable long-term memories
5. **Fix thread safety in DB access** → Prevent corruption
**Severity:** MEDIUM
**Location:** `get_summary_context()` (Line ~1550)
**Issue:** Cache key based only on user input

```python
def get_summary_context(self, user_input=""):
    cache_key = user_input[:50] if user_input else ""

    if self._summary_context_cache is not None and self._summary_context_cache.get("key") == cache_key:
        return self._summary_context_cache.get("value", "")
```

**Problem:** Same input across turns returns wrong cached summary.

**Fix:** Include turn counter in cache key.

---

## 🟢 Low-Priority Issues

### 7. **Unbounded Memory Buffer Growth**
**Severity:** LOW-MEDIUM
**Location:** `buffer_memory_candidate()` (Line ~1100-1130)
**Issue:** Buffer could grow if flush fails

```python
def buffer_memory_candidate(self, kind, value, saliency=None):
    self.memory_buffer.append({...})

    if len(self.memory_buffer) > 24:
        self.memory_buffer = self.memory_buffer[-24:]
```

**Problem:** If `flush_memory_buffer()` fails, buffer accumulates.

---

## 📋 Summary

| Severity | Count | Issues |
|----------|-------|--------|
| 🔴 Critical | 1 | Memory buffer flush logic |
| 🔴 High | 2 | Embedding performance, Proactive race condition |
| 🟠 Medium-High | 2 | Memory consolidation, Thread safety |
| 🟡 Medium | 2 | Cache logic, Buffer growth |
| 🟢 Low | 1 | Minor issues |

## ✅ Recommended Fixes (Priority Order)

1. **Fix memory buffer flush logic** → Reduce unnecessary AI calls
2. **Implement lazy embedding computation** → Improve performance
3. **Add proactive message race protection** → Prevent double-sends
4. **Add importance field to memory system** → Preserve valuable long-term memories
5. **Fix thread safety in DB access** → Prevent corruption
    """Connection reuse without full lock protection"""
    if self._db_connection is not None:
        try:
            self._db_connection.execute("SELECT 1")
            return self._db_connection
        except sqlite3.Error:
            self._db_connection = None
    
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)  # ❌ check_same_thread=False is risky
    # ... rest of initialization ...
    self._db_connection = conn
    return conn
```

**Problem:** `check_same_thread=False` allows unfettered access, and connection isn't protected during initialization.

**Fix:** Move initialization inside the lock or use proper connection pooling.

---

### 5. **Memory Persistence Bug: `save_memory()` Only on Dirty Flag** (Line ~745)
**Severity:** MEDIUM
**Issue:** Memory changes may not persist if `_is_dirty` flag isn't set

```python
def save_memory(self):
    if not self._is_dirty:
        return  # ❌ Skip save if dirty flag not set
```

**Problem:** Some methods modify memory but don't set `_is_dirty = True`. Example:
- `extract_memory()` modifies `self.memory["memory_buffer"]` but doesn't always set dirty flag before calling `extract_prompt`
- `update_emotion()` modifies memory but calling `save_memory()` depends on dirty flag being set elsewhere

**Risk:** Memory changes are lost if session crashes.

---

### 6. **Cache Key Logic Error in `get_summary_context()`** (Line ~1550)
**Severity:** MEDIUM
**Issue:** Cache key based on user input - but same input across turns gives wrong cache

```python
def get_summary_context(self, user_input=""):
    cache_key = user_input[:50] if user_input else ""
    
    if self._summary_context_cache is not None and self._summary_context_cache.get("key") == cache_key:
        return self._summary_context_cache.get("value", "")
```

**Problem:** If user sends "Hello" twice on different turns, both will return the same cached summary (incorrect).

**Fix:** Include conversation turn number in cache key:
```python
cache_key = f"{self.turn_counter}_{user_input[:50]}" if user_input else f"{self.turn_counter}_empty"
```

---

### 7. **Integer Overflow Risk in `access_count`** (Line ~850-860)
**Severity:** LOW-MEDIUM
**Issue:** `access_count` incremented without bounds

```python
def touch_memory_items(self, items):
    # ... line ~1450 ...
    c.execute(
        "UPDATE memory_items SET last_used_at=?, access_count=access_count+1 "
        "WHERE kind=? AND value=?",
        (now, db_kind, value)
    )
```

**Problem:** `access_count` can grow indefinitely. After millions of turns, SQLite integer could overflow.

**Fix:** Cap the value:
```python
c.execute(
    "UPDATE memory_items SET last_used_at=?, access_count=MIN(access_count+1, 1000) "
    "WHERE kind=? AND value=?",
    (now, db_kind, value)
)
```

---

**Problem:** If `flush_memory_buffer()` fails, buffer accumulates.

---

## 📋 Summary

| Severity | Count | Issues |
|----------|-------|--------|
| 🔴 Critical | 1 | Memory buffer flush logic |
| 🔴 High | 2 | Embedding performance, Proactive race condition |
| 🟠 Medium-High | 2 | Memory consolidation, Thread safety |
| 🟡 Medium | 2 | Cache logic, Buffer growth |
| 🟢 Low | 1 | Minor issues |

## ✅ Recommended Fixes (Priority Order)

1. **Fix memory buffer flush logic** → Reduce unnecessary AI calls
2. **Implement lazy embedding computation** → Improve performance
3. **Add proactive message race protection** → Prevent double-sends
4. **Add importance field to memory system** → Preserve valuable long-term memories
5. **Fix thread safety in DB access** → Prevent corruption

### 9. **SQL Injection-like Vulnerability in `estimate_memory_saliency()`** (No direct SQL, but style issue)
**Severity:** LOW
**Issue:** String comparison in Python could be unsafe if user data isn't validated

**Problem:** If `flush_memory_buffer()` fails, buffer accumulates.

---

## 📋 Summary

| Severity | Count | Issues |
|----------|-------|--------|
| 🔴 Critical | 1 | Memory buffer flush logic |
| 🔴 High | 2 | Embedding performance, Proactive race condition |
| 🟠 Medium-High | 2 | Memory consolidation, Thread safety |
| 🟡 Medium | 2 | Cache logic, Buffer growth |
| 🟢 Low | 1 | Minor issues |

## ✅ Recommended Fixes (Priority Order)

1. **Fix memory buffer flush logic** → Reduce unnecessary AI calls
2. **Implement lazy embedding computation** → Improve performance
3. **Add proactive message race protection** → Prevent double-sends
4. **Add importance field to memory system** → Preserve valuable long-term memories
5. **Fix thread safety in DB access** → Prevent corruption

### 10. **Missing Error Handling in Database Operations** (Line ~480-510)
**Severity:** MEDIUM
**Issue:** Database connection failures not propagated cleanly

```python
def _get_db(self):
    # ... line ~507 ...
    except Exception as e:
        print(f"[Core] DB Connection Error: {e}")
        return None  # ❌ Returns None without fallback
```

Later code assumes `conn` is valid:
```python
def load_memory(self):
    conn = self._get_db()
    if not conn:  # Good check here
        return self.get_default_memory()
    
    c = conn.cursor()  # But other methods assume conn exists
```

**Fix:** Add defensive checks in all methods that use `_get_db()`.

---

### 11. **Unbounded Memory Buffer Growth** (Line ~1100-1130)
**Severity:** LOW-MEDIUM
**Issue:** Memory buffer could theoretically grow uncontrolled

```python
def buffer_memory_candidate(self, kind, value, saliency=None):
    # ... line ~1120 ...
    self.memory_buffer.append({...})
    
    if len(self.memory_buffer) > 24:
        self.memory_buffer = self.memory_buffer[-24:]
```

**Problem:** While there's a cap at 24, if `flush_memory_buffer()` fails/throws, buffer won't be cleared and could accumulate.

---

## 🟢 Low-Priority Issues / Code Smells

### 12. **Redundant Memory Assignments** (Line ~900, 1100+)
**Severity:** LOW (Code smell)
**Issue:** Unnecessary reassignments

```python
self.memory_buffer.append(item)
self.memory["memory_buffer"] = self.memory_buffer  # ❌ Redundant - same reference
```

**Fix:** Remove redundant assignments.

---

**Problem:** If `flush_memory_buffer()` fails, buffer accumulates.

---

## 📋 Summary

| Severity | Count | Issues |
|----------|-------|--------|
| 🔴 Critical | 1 | Memory buffer flush logic |
| 🔴 High | 2 | Embedding performance, Proactive race condition |
| 🟠 Medium-High | 2 | Memory consolidation, Thread safety |
| 🟡 Medium | 2 | Cache logic, Buffer growth |
| 🟢 Low | 1 | Minor issues |

## ✅ Recommended Fixes (Priority Order)

1. **Fix memory buffer flush logic** → Reduce unnecessary AI calls
2. **Implement lazy embedding computation** → Improve performance
3. **Add proactive message race protection** → Prevent double-sends
4. **Add importance field to memory system** → Preserve valuable long-term memories
5. **Fix thread safety in DB access** → Prevent corruption
