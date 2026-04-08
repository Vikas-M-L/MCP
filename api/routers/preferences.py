"""
Preferences endpoints — ChromaDB user preference management.
  GET  /api/preferences  → list all stored preference statements
  POST /api/preferences  → add a new natural-language preference
"""
import asyncio

from fastapi import APIRouter
from fastapi.responses import JSONResponse

router = APIRouter()


@router.get("/api/preferences")
async def list_preferences() -> list[dict]:
    """List all ChromaDB user preference documents."""
    from memory.chroma_memory import ChromaMemory
    memory = ChromaMemory.from_settings()
    try:
        count = await asyncio.to_thread(memory.preferences.count)
        if count == 0:
            return []
        result = await asyncio.to_thread(
            memory.preferences.get,
            limit=count,
            include=["documents", "metadatas"],
        )
        return [
            {"document": doc, "metadata": meta}
            for doc, meta in zip(
                result.get("documents", []),
                result.get("metadatas", []),
            )
        ]
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.post("/api/preferences")
async def add_preference(body: dict) -> dict:
    """Add a new natural-language preference to ChromaDB."""
    text = (body.get("text") or "").strip()
    if not text:
        return JSONResponse({"error": "text is required"}, status_code=400)
    category = body.get("category", "general")
    from memory.chroma_memory import ChromaMemory
    memory = ChromaMemory.from_settings()
    try:
        await memory.store_preference(text, category=category, source="dashboard")
        return {"status": "saved", "text": text, "category": category}
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)
