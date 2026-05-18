import os
import requests
import threading
import pytz
from datetime import datetime
try:
    import numpy as np
except ImportError:
    np = None

# Reuse TCP connections for frequent local embedding calls.
_EMBED_SESSION = requests.Session()

# Constants
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "memory.db")
DB_LOCK = threading.Lock()

LAYER_USER = "user"      # L1 — persistent facts
LAYER_SESSION = "session"  # L2 — stream-specific context
LAYER_TEMPORAL = "temporal" # L3 — episodic/summaries

_LAYER_MAP = {
    "like": LAYER_USER,
    "dislike": LAYER_USER,
    "goal": LAYER_USER,
    "topic": LAYER_USER,
    "relational": LAYER_USER,
    "inside_joke": LAYER_USER,
    "episodic": LAYER_TEMPORAL,
    "rl_few_shot": LAYER_TEMPORAL,
    "session": LAYER_SESSION,
}

_CONFLICTABLE_KINDS = {"like", "dislike", "goal", "relational"}

KIND_IMPORTANCE = {
    "relational": 1.5,
    "goal": 1.4,
    "inside_joke": 1.3,
    "like": 1.2,
    "dislike": 1.2,
    "topic": 1.1,
    "episodic": 1.0,
    "rl_few_shot": 1.6,
    "session": 0.9
}

def _get_ollama_embedding(text: str) -> "np.ndarray | None":
    """Calls Ollama to get vector embedding."""
    if np is None: return None
    try:
        from config import EMBEDDING_MODEL, EMBEDDING_URL
        resp = _EMBED_SESSION.post(EMBEDDING_URL, json={"model": EMBEDDING_MODEL, "prompt": text}, timeout=10)
        if resp.status_code == 200:
            vec = resp.json().get("embedding")
            if vec: return np.array(vec, dtype=np.float32)
    except Exception:
        pass
    return None

def configure_sqlite_connection(conn) -> None:
    """Apply the repo's SQLite runtime settings to every memory.db connection."""
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA cache_size=-32000")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=MEMORY")

def _cosine_similarity(v1, v2) -> float:
    """Calculates cosine similarity between two vectors."""
    if v1 is None or v2 is None or np is None: return 0.0
    dot = np.dot(v1, v2)
    norm1, norm2 = np.linalg.norm(v1), np.linalg.norm(v2)
    if norm1 == 0 or norm2 == 0: return 0.0
    return float(dot / (norm1 * norm2))

def _vectorized_cosine_similarity(query_vec: "np.ndarray", matrix: "np.ndarray") -> "np.ndarray":
    """Calculates cosine similarity between a 1D query vector and a 2D matrix of vectors."""
    if query_vec is None or matrix is None or np is None or len(matrix) == 0:
        return np.array([])
    # Compute dot products
    dot_products = np.dot(matrix, query_vec)
    # Compute norms
    query_norm = np.linalg.norm(query_vec)
    matrix_norms = np.linalg.norm(matrix, axis=1)
    # Avoid division by zero
    denominators = query_norm * matrix_norms
    # Handle zero norms
    similarities = np.divide(dot_products, denominators, out=np.zeros_like(dot_products), where=denominators!=0)
    return similarities

def get_now_vn():
    """Returns the current time in Asia/Ho_Chi_Minh timezone."""
    return datetime.now(pytz.timezone("Asia/Ho_Chi_Minh"))
