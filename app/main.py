"""FastAPI server: chat API plus the web page.

    uvicorn app.main:app --reload
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from pymongo import MongoClient

from . import config
from .agent import HSEAgent, sse
from .tools import category_values

STATIC = Path(__file__).parent / "static"


class ChatRequest(BaseModel):
    conversation_id: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=8000)


def create_app(collection=None, client=None) -> FastAPI:
    """`collection` / `client` let tests pass a fake database and Claude client."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        mongo = None
        coll = collection
        if coll is None:
            mongo = MongoClient(config.MONGODB_URI, serverSelectionTimeoutMS=10000, appname="hse-chatbot")
            coll = mongo[config.MONGODB_DB][config.MONGODB_COLLECTION]
        values = category_values(coll)
        app.state.coll = coll
        app.state.agent = HSEAgent(coll, values, client=client)
        yield
        if mongo is not None:
            mongo.close()

    app = FastAPI(title="HSE Chatbot", lifespan=lifespan)

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/health")
    async def health():
        return {"status": "ok", "incidents": app.state.coll.estimated_document_count()}

    @app.post("/api/chat")
    async def chat(req: ChatRequest):
        async def events():
            async for event in app.state.agent.chat(req.conversation_id, req.message):
                yield sse(event)

        return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    @app.delete("/api/conversations/{conversation_id}")
    async def reset(conversation_id: str):
        if app.state.agent.conversations.pop(conversation_id, None) is None:
            raise HTTPException(404, "No such conversation")
        return {"status": "cleared"}

    return app


app = create_app()
