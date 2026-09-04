"""The levelup.peoples shopping assistant service.

    uvicorn levelup_shopping.main:app --port 8000

    POST   /api/session     bind a session to the principal, return its id
    POST   /api/chat        one turn, streamed as SSE AgentEvents
    GET    /api/cart        the session's cart (plus the checkout handoff)
    GET    /api/orders      the session customer's orders, newest first
    GET/DELETE /api/memory  what is remembered about the session's customer
    POST   /api/reset       drop the session, start a fresh one
    GET    /api/health      (public)

After session start every request carries ``X-Session-Id`` alone; no request field or
tool argument names a customer. Run one worker: the session store and the backend's
cart map live in this process (see CLAUDE.md, decision record, Sessions).
"""

# Route parameters are annotated with dependencies built at import time, so this module
# evaluates its annotations eagerly (no ``from __future__ import annotations``).

from datetime import datetime
from pathlib import Path
from typing import Any, cast

from commerce_common.memory import JsonFileMemoryStore, MemoryStore
from dotenv import load_dotenv
from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from shopping_agent import PageContext, ShoppingSessionContext, ShoppingSessionState
from shopping_agent.serialization import cart_payload
from shopping_agent_runtime import ShoppingAgent

from .agent_config import build_config, build_credentials, dev_customer_id
from .backend import ShopifyStorefrontBackend
from .executor import LevelupToolExecutor
from .host import append_user_turn, build_app, stream_turn
from .sessions import SessionStore, session_dependency

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SKILLS_DIR = Path(__file__).resolve().parent / "skills"
DATA_DIR = PROJECT_ROOT / "data"

load_dotenv(PROJECT_ROOT / ".env", override=False)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    page: PageContext | None = None


class MemoryFactRef(BaseModel):
    key: str = Field(min_length=1, max_length=64)


def build_agent(backend: ShopifyStorefrontBackend, **overrides: Any) -> ShoppingAgent:
    DATA_DIR.mkdir(exist_ok=True)
    return ShoppingAgent(
        backend=backend,
        skills_dir=SKILLS_DIR,
        config=overrides.pop("config", build_config()),
        memory_store=overrides.pop(
            "memory_store", JsonFileMemoryStore(DATA_DIR / ".memory-store.json")
        ),
        executor_class=LevelupToolExecutor,
        **overrides,
    )


def create_app(backend: ShopifyStorefrontBackend, agent: ShoppingAgent):
    app = build_app("Levelup shopping assistant")
    sessions: SessionStore[ShoppingSessionState] = SessionStore(ShoppingSessionState)
    CurrentSession = session_dependency(sessions, "/api/session")
    memory_store = cast(MemoryStore, agent.memory.store)

    def context(record, page: PageContext | None = None) -> ShoppingSessionContext:
        # Identity from the record; the clock is the server's until the storefront
        # passes the customer's IANA timezone here. TODO
        return ShoppingSessionContext(
            session_id=record.session_id,
            user_id=record.user_id,
            page=page or PageContext(),
            now=datetime.now(),
        )

    @app.post("/api/session")
    async def start_session() -> dict:
        # TODO: authenticate the caller and pass the customer GID sign-in resolved.
        record = sessions.start(dev_customer_id())
        profile = await backend.get_preferences(context(record))
        return {"session_id": record.session_id, "name": profile.display_name}

    @app.post("/api/chat")
    async def chat(request: ChatRequest, record: CurrentSession) -> StreamingResponse:
        append_user_turn(record, request.message, "App events")
        return stream_turn(agent, sessions, record, context(record, request.page))

    @app.get("/api/cart")
    async def get_cart(record: CurrentSession) -> dict:
        session = context(record)
        cart = await backend.get_cart(session)
        handoff = await backend.checkout_handoff(session, cart)
        return cart_payload(cart) | {"checkout": [h.model_dump() for h in handoff]}

    @app.get("/api/orders")
    async def list_orders(record: CurrentSession) -> dict:
        orders = await backend.get_orders(context(record), limit=20)
        return {"orders": [order.model_dump(mode="json") for order in orders]}

    @app.get("/api/memory")
    async def get_memory(record: CurrentSession) -> dict:
        facts = await memory_store.get_facts(record.user_id)
        return {"facts": [fact.model_dump(mode="json") for fact in facts]}

    @app.delete("/api/memory")
    async def delete_memory_fact(ref: MemoryFactRef, record: CurrentSession) -> dict:
        if not await memory_store.delete_fact(record.user_id, ref.key):
            raise HTTPException(status_code=404, detail="No such fact")
        return {"ok": True, "deleted": ref.key}

    @app.post("/api/reset")
    async def reset(record: CurrentSession) -> dict:
        sessions.reset(record)
        backend.reset_session(record.session_id)
        fresh = sessions.start(record.user_id)
        return {"ok": True, "session_id": fresh.session_id}

    @app.get("/api/health")
    async def health() -> dict:
        return {"ok": True, "skills": agent.skills.names, "model": agent.config.model}

    app.state.sessions = sessions
    return app


backend = ShopifyStorefrontBackend(build_credentials())
agent = build_agent(backend)
app = create_app(backend, agent)
