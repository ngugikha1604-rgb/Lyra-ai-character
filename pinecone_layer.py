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
            resp = requests.get(f"https://api.pinecone.io/indexes/{self.index_name}", headers={"Api-Key": self.api_key}, timeout=10)
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
            resp = requests.post(
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
        try:
            requests.post(
                f"https://{host}/vectors/upsert",
                headers={"Api-Key": self.api_key, "Content-Type": "application/json"},
                json={"vectors": [{"id": item_id, "values": vector, "metadata": metadata}]},
                timeout=10
            )
        except Exception as e:
            print(f"[Pinecone] upsert error: {e}")

    def query(self, vector: list, top_k: int = 6, filter_meta: dict = None) -> list:
        """Semantic search in Pinecone."""
        host = self._get_host()
        if not host or not self._enabled: return []
        try:
            body = {"vector": vector, "topK": top_k, "includeMetadata": True}
            if filter_meta: body["filter"] = filter_meta
            resp = requests.post(
                f"https://{host}/query",
                headers={"Api-Key": self.api_key, "Content-Type": "application/json"},
                json=body,
                timeout=10
            )
            if resp.status_code == 200:
                return [{"id": m["id"], "score": m["score"], "metadata": m.get("metadata", {})} for m in resp.json().get("matches", [])]
        except Exception as e:
            print(f"[Pinecone] query error: {e}")
        return []

    def delete(self, item_id: str):
        """Deletes a vector from Pinecone."""
        host = self._get_host()
        if not host or not self._enabled: return
        try:
            requests.post(f"https://{host}/vectors/delete", headers={"Api-Key": self.api_key, "Content-Type": "application/json"}, json={"ids": [item_id]}, timeout=10)
        except Exception as e:
            print(f"[Pinecone] delete error: {e}")
