"""Process-level plumbing, from the reference's ``examples/demo_common/host.py``: the app
with its host and CORS middleware, background tasks, and the SSE response one chat turn
streams. The record is written back when the stream ends; routes never call ``save``."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Sequence
from contextlib import asynccontextmanager
from typing import Any, Protocol

import anthropic
from commerce_common.streaming import AgentEvent, to_sse
from commerce_common.turn import session_tag
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .sessions import SessionConflictError, SessionRecord, SessionStore

logger = logging.getLogger(__name__)

_background_tasks: set[asyncio.Task[Any]] = set()


def spawn_background(coro: Coroutine[Any, Any, object]) -> None:
    task = asyncio.get_running_loop().create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


def _lifespan(on_startup: Sequence[Callable[[], Awaitable[None]]]):
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
            logger.info(
                "No ANTHROPIC_API_KEY in the environment; the SDK credential chain applies."
            )
        for step in on_startup:
            await step()
        yield

    return lifespan


def build_app(title: str, on_startup: Sequence[Callable[[], Awaitable[None]]] = ()) -> FastAPI:
    """Answers only to loopback host names (plus ``ALLOWED_HOSTS``, for a deployment that
    puts its own authentication in front) and to any localhost origin."""
    logging.basicConfig(
        level=os.environ.get("DEMO_LOG_LEVEL", "INFO").upper(),
        format="%(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    extra_hosts = [
        host.strip().rsplit(":", 1)[0] if ":" in host.strip() else host.strip()
        for host in os.environ.get("ALLOWED_HOSTS", "").split(",")
    ]
    app = FastAPI(title=title, version="0.1.0", lifespan=_lifespan(on_startup))
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["localhost", "127.0.0.1", "testserver", *(h for h in extra_hosts if h)],
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"http://(localhost|127\.0\.0\.1):\d+",
        allow_methods=["*"],
        allow_headers=["*"],
    )
    return app


class TurnAgent(Protocol):
    def stream_turn(
        self, messages: list[dict[str, Any]], session: Any, state: Any
    ) -> AsyncIterator[AgentEvent]: ...

    async def update_memory(self, messages: list[dict[str, Any]], session: Any) -> Any: ...


def append_user_turn(record: SessionRecord[Any], message: str, events_label: str) -> None:
    """Add the user's message, preceded by a note listing what happened outside the
    conversation since the last reply, when anything did."""
    if not record.pending_app_events:
        record.messages.append({"role": "user", "content": message})
        return
    note = f"[{events_label} since your last reply: " + " ".join(record.pending_app_events) + "]"
    record.pending_app_events.clear()
    record.messages.append(
        {
            "role": "user",
            "content": [{"type": "text", "text": note}, {"type": "text", "text": message}],
        }
    )


def stream_turn(
    agent: TurnAgent,
    sessions: SessionStore[Any],
    record: SessionRecord[Any],
    session: Any,
) -> StreamingResponse:
    """Stream one turn as SSE; the record is written back once the stream has ended.
    Credential failures become a readable error event; anything else is logged and
    reported generically. Memory extraction runs after the response has streamed."""

    async def event_stream() -> AsyncIterator[str]:
        try:
            async for event in agent.stream_turn(record.messages, session, record.state):
                if event.type == "turn_complete" and event.data.get("results_cleared"):
                    record.stored_messages = 0
                yield to_sse(event)
        except anthropic.AuthenticationError:
            logger.exception("chat turn failed: API authentication")
            yield to_sse(
                AgentEvent.error("Anthropic API authentication failed; check ANTHROPIC_API_KEY.")
            )
        except Exception:
            logger.exception("chat turn failed")
            yield to_sse(AgentEvent.error("Something went wrong on our side. Please try again."))
        else:
            spawn_background(agent.update_memory(record.messages, session))

    def write_back() -> None:
        try:
            sessions.save(record)
        except SessionConflictError:
            record.version = (sessions.read_state(record.session_id) or (0, {}))[0]
            logger.warning(
                "session %s: a write raced the turn; the turn wins", session_tag(record.session_id)
            )
            sessions.save(record)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        background=BackgroundTask(write_back),
    )
