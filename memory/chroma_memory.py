"""
ChromaDB vector memory — two collections:
  user_preferences  — semantic preference statements
  action_outcomes   — past action approval/rejection history

All chroma calls are wrapped in asyncio.to_thread() since chromadb
PersistentClient is synchronous and would block the event loop.
"""
import asyncio
import os
import uuid
from datetime import datetime, timezone
from typing import Any

import chromadb
from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction

# Apply HuggingFace token so model downloads are authenticated (higher rate limits)
_hf_token = os.getenv("HUGGINGFACE_TOKEN") or os.getenv("HF_TOKEN")
if _hf_token:
    os.environ["HF_TOKEN"] = _hf_token


class ChromaMemory:
    def __init__(self, persist_path: str, embedding_model: str) -> None:
        self._client = chromadb.PersistentClient(path=persist_path)
        self._ef = SentenceTransformerEmbeddingFunction(
            model_name=embedding_model,
            device="cpu",
            normalize_embeddings=True,
        )
        self.preferences = self._client.get_or_create_collection(
            name="user_preferences",
            embedding_function=self._ef,
            metadata={"hnsw:space": "cosine"},
        )
        self.outcomes = self._client.get_or_create_collection(
            name="action_outcomes",
            embedding_function=self._ef,
            metadata={"hnsw:space": "cosine"},
        )

    # ── Factory ───────────────────────────────────────────────────────────────

    @classmethod
    def from_settings(cls) -> "ChromaMemory":
        from config.settings import get_settings
        cfg = get_settings()
        return cls(cfg.chroma_persist_path, cfg.chroma_embedding_model)

    # ── User Preferences ──────────────────────────────────────────────────────

    async def store_preference(
        self,
        text: str,
        category: str = "general",
        source: str = "inferred",
        priority: int = 2,
    ) -> None:
        """
        Store a natural-language preference statement as an embedding.
        Example: "User always replies to emails from professor@uni.edu within 2 hours"
        """
        doc_id = f"pref-{uuid.uuid4()}"
        meta = {
            "category": category,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source": source,
            "priority": priority,
        }
        await asyncio.to_thread(
            self.preferences.add,
            documents=[text],
            metadatas=[meta],
            ids=[doc_id],
        )

    async def query_preferences(
        self, query: str, n_results: int = 3
    ) -> list[dict[str, Any]]:
        """
        Semantic search over stored preferences.
        Returns list of {document, metadata, distance}.
        """
        results = await asyncio.to_thread(
            self.preferences.query,
            query_texts=[query],
            n_results=min(n_results, max(1, self.preferences.count())),
        )
        output = []
        if results and results["documents"]:
            for doc, meta, dist in zip(
                results["documents"][0],
                results["metadatas"][0],
                results["distances"][0],
            ):
                output.append({"document": doc, "metadata": meta, "distance": dist})
        return output

    # ── Action Outcomes ───────────────────────────────────────────────────────

    async def record_outcome(
        self,
        plan: dict[str, Any],
        result: dict[str, Any],
        approved: bool,
        executor: str = "auto",
    ) -> None:
        """
        Store the outcome of an action for future approval rate calculation.
        Document text: "<event_type> event — action: <action>" for semantic similarity.
        """
        doc_id = f"outcome-{uuid.uuid4()}"
        action_type = plan.get("action", "unknown")
        event_type = plan.get("event_type", "unknown")
        document = f"{event_type} event — action: {action_type}"
        meta = {
            "action_type": action_type,
            "approved": approved,
            "confidence_at_time": int(plan.get("confidence", 0)),
            "event_type": event_type,
            "executor": executor,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        await asyncio.to_thread(
            self.outcomes.add,
            documents=[document],
            metadatas=[meta],
            ids=[doc_id],
        )

    async def get_approval_rate(self, action_type: str) -> float:
        """
        Query outcomes for this action_type and return historical approval rate.
        Returns 0.5 (neutral) when no history exists.
        """
        count = await asyncio.to_thread(self.outcomes.count)
        if count == 0:
            return 0.5

        try:
            results = await asyncio.to_thread(
                self.outcomes.query,
                query_texts=[f"action: {action_type}"],
                n_results=min(20, count),
                where={"action_type": {"$eq": action_type}},
            )
        except Exception:
            # where filter may fail if no matching docs
            return 0.5

        if not results or not results["metadatas"] or not results["metadatas"][0]:
            return 0.5

        metas = results["metadatas"][0]
        if not metas:
            return 0.5

        approved_count = sum(1 for m in metas if m.get("approved", False))
        return approved_count / len(metas)

    async def seed_default_preferences(self) -> None:
        """
        Seed sensible default preferences on first run so the planner
        has context even before the user has a history.
        """
        count = await asyncio.to_thread(self.preferences.count)
        if count > 0:
            return  # already seeded

        defaults = [
            ("User prioritizes emails from professors, managers, and important contacts", "email", 1),
            ("User wants to be notified immediately about urgent deadlines", "general", 1),
            ("User prefers to review and approve calendar changes before they are made", "calendar", 2),
            ("User's download folder should be organized automatically when it has more than 20 files", "filesystem", 2),
            ("User values concise, professional email replies", "email", 2),
        ]
        for text, category, priority in defaults:
            await self.store_preference(text, category=category, priority=priority, source="default")
