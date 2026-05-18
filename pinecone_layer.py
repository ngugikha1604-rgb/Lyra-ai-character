import requests
from memory_utils import _get_ollama_embedding

class PineconeLayer:
    """
    Thin wrapper around Pinecone REST API.
    Handles L3 Temporal memory (episodic + summaries).
    """
    def __init__(self):
        from config import PINECONE_API_KEY, PINECONE_INDEX
        self.api_key = PINECONE_API_KEY
        self.index_name = PINECONE_INDEX
        self._host = None  # lazy-loaded
        self._enabled = bool(self.api_key)
        self.dimension = None
        self._session = requests.Session()
        if self._enabled:
            self._detect_dimension()

    def _detect_dimension(self):
        """Auto-detects dimension by probing the embedding model."""
        try:
            sample_vec = _get_ollama_embedding("probe")
            if sample_vec is not None:
                self.dimension = len(sample_vec)
                print(f"[Pinecone] Auto-detected dimension: {self.dimension}")
        except Exception as e:
            print(f"[Pinecone] Dimension detection error: {e}")

    def _get_host(self) -> str:
        """Retrieves and caches the host URL for the index."""
        if self._host: return self._host
        if not self._enabled: return None
        try:
            resp = self._session.get(f"https://api.pinecone.io/indexes/{self.index_name}", headers={"Api-Key": self.api_key}, timeout=10)
            if resp.status_code == 200:
                self._host = resp.json().get("host")
                return self._host
            elif resp.status_code == 404:
                self._create_index()
                return self._get_host()
        except Exception as e:
            print(f"[Pinecone] get_host error: {e}")
        return None

    def _create_index(self):
        """Creates a serverless index if it doesn't exist."""
        try:
            resp = self._session.post(
                "https://api.pinecone.io/indexes",
                headers={"Api-Key": self.api_key, "Content-Type": "application/json"},
                json={
                    "name": self.index_name,
                    "dimension": self.dimension or 768,
                    "metric": "cosine",
                    "spec": {"serverless": {"cloud": "aws", "region": "us-east-1"}},
                },
                timeout=30
            )
            if resp.status_code in (200, 201):
                print(f"[Pinecone] Index '{self.index_name}' created.")
        except Exception as e:
            print(f"[Pinecone] create_index error: {e}")

    def upsert(self, item_id: str, vector: list, metadata: dict):
        """Upserts a single vector into Pinecone."""
        host = self._get_host()
        if not host or not self._enabled: return
        
        # Ensure timestamp exists for temporal ranking
        if "timestamp" not in metadata:
            from datetime import datetime, timezone
            metadata["timestamp"] = datetime.now(timezone.utc).isoformat()
            
        try:
            self._session.post(
                f"https://{host}/vectors/upsert",
                headers={"Api-Key": self.api_key, "Content-Type": "application/json"},
                json={"vectors": [{"id": item_id, "values": vector, "metadata": metadata}]},
                timeout=10
            )
        except Exception as e:
            print(f"[Pinecone] upsert error: {e}")

    def query(self, vector: list, top_k: int = 6, filter_meta: dict = None) -> list:
        """Semantic search in Pinecone with Zep-style temporal re-ranking."""
        host = self._get_host()
        if not host or not self._enabled: return []
        try:
            fetch_k = top_k * 3
            body = {"vector": vector, "topK": fetch_k, "includeMetadata": True}
            if filter_meta: body["filter"] = filter_meta
            resp = self._session.post(
                f"https://{host}/query",
                headers={"Api-Key": self.api_key, "Content-Type": "application/json"},
                json=body,
                timeout=10
            )
            if resp.status_code == 200:
                # score_raw = cosine gốc từ Pinecone (không bị ảnh hưởng bởi temporal decay)
                matches = [{"id": m["id"], "score": m["score"], "score_raw": m["score"], "metadata": m.get("metadata", {})} for m in resp.json().get("matches", [])]
                
                # Zep-style temporal ranking
                from datetime import datetime, timezone
                now = datetime.now(timezone.utc)
                
                for m in matches:
                    ts_str = m["metadata"].get("timestamp")
                    time_diff_hours = 24.0 * 30  # Default to 30 days old if no timestamp
                    if ts_str:
                        try:
                            # Handle both timezone-aware and naive ISO formats
                            ts = datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
                            if ts.tzinfo is None:
                                ts = ts.replace(tzinfo=timezone.utc)
                            time_diff_hours = max(0, (now - ts).total_seconds() / 3600.0)
                        except Exception:
                            pass
                    
                    # Freshness weight: Halves every 48 hours, floors at 0.15
                    # score_raw giữ nguyên cosine gốc; score dùng temporal decay để sort
                    freshness = max(0.15, 1.0 / ((time_diff_hours / 48.0) + 1.0))
                    m["score"] = m["score_raw"] * freshness
                
                # FIX: Filter bằng score_raw (cosine thực), không phải post-decay score.
                # Bug cũ: memory 30 ngày tuổi có cosine 0.9 → post-decay 0.135 → bị filter mất.
                matches = [m for m in matches if m["score_raw"] > 0.45]
                matches.sort(key=lambda x: x["score"], reverse=True)
                return matches[:top_k]
        except Exception as e:
            print(f"[Pinecone] query error: {e}")
        return []

    def delete(self, item_id: str):
        """Deletes a vector from Pinecone."""
        host = self._get_host()
        if not host or not self._enabled: return
        try:
            self._session.post(f"https://{host}/vectors/delete", headers={"Api-Key": self.api_key, "Content-Type": "application/json"}, json={"ids": [item_id]}, timeout=10)
        except Exception as e:
            print(f"[Pinecone] delete error: {e}")
