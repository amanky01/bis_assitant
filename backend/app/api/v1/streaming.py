"""SSE keep-alives with bounded buffering and producer cancellation."""
import asyncio
from collections.abc import AsyncGenerator
from contextlib import aclosing, suppress
from time import monotonic

from app.core.telemetry import event
from app.schemas.chat import StreamChunk


def frame(kind: str, content: str = "") -> str:
    return f"data: {StreamChunk(type=kind, content=content).model_dump_json()}\n\n"


async def keep_alive(
    stream: AsyncGenerator[str, None], interval: float = 10
) -> AsyncGenerator[str, None]:
    queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=1)
    start = monotonic()
    first_ms = None
    outcome = "disconnected"

    async def produce():
        try:
            async with aclosing(stream):
                async for value in stream:
                    await queue.put(value)
        except Exception:
            await queue.put(frame("error", "Unable to complete this response."))
        await queue.put(None)

    task = asyncio.create_task(produce())
    try:
        while True:
            try:
                value = await asyncio.wait_for(queue.get(), timeout=interval)
            except TimeoutError:
                yield ": keep-alive\n\n"
                continue
            if value is None:
                if outcome == "disconnected":
                    outcome = "incomplete"
                break
            chunk = StreamChunk.model_validate_json(value.removeprefix("data: ").strip())
            if chunk.type == "token" and first_ms is None:
                first_ms = round((monotonic() - start) * 1000)
            if chunk.type in {"done", "error"}:
                outcome = "completed" if chunk.type == "done" else "error"
            yield value
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        event("chat_stream", outcome=outcome, first_token_ms=first_ms,
              duration_ms=round((monotonic() - start) * 1000))
