"""Provides a decorator for debouncing function calls."""

import asyncio
from collections.abc import Callable, Coroutine
import contextlib
from typing import Any, Protocol, cast


class DebouncedCallable(Protocol):
    """Protocol for a debounced callable with a close method."""

    async def __call__(self, *args: Any, **kwargs: Any) -> None:
        """Call the debounced function."""

    async def close(self) -> None:
        """Close the debounced callable."""


class Debounce:
    """Debounce class to limit the frequency of a function call."""

    def __init__(self, delay: float) -> None:
        """Initialize the Debounce class with a delay."""
        self._delay = delay
        self._queue: asyncio.Queue[tuple[Any, Any]] = asyncio.Queue()
        self._task: asyncio.Task[Any] | None = None
        self._is_closing: bool = False

    def __call__(
        self, func: Callable[..., Coroutine[Any, Any, None]]
    ) -> DebouncedCallable:
        """Wrap the function with a debounced version."""

        async def debounced_wrapper(*args: Any, **kwargs: Any) -> None:
            """Debounce the wrapped callable."""
            if self._is_closing:
                return
            await self._queue.put((args, kwargs))
            if self._task is None or self._task.done():
                self._task = asyncio.create_task(self._process_queue(func))

        async def close() -> None:
            """Expose a close method to stop the debounce."""
            self._is_closing = True
            if self._task:
                self._task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._task

        # Adding the close method to the debounced wrapper, then cast to meet the protocol
        setattr(debounced_wrapper, "close", close)
        return cast(DebouncedCallable, debounced_wrapper)  # Explicit cast

    async def _process_queue(
        self, func: Callable[..., Coroutine[Any, Any, None]]
    ) -> None:
        """Process the queue with a delay."""
        while not self._queue.empty():
            args, kwargs = await self._queue.get()
            with contextlib.suppress(Exception):
                await func(*args, **kwargs)
            await asyncio.sleep(self._delay)
