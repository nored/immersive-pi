"""Track starts from HTTP, WebSocket and CLI so Stop cancels the same operation."""
import asyncio
from functools import wraps


def managed_start(method):
    @wraps(method)
    async def run(self, *args, **kwargs):
        current = asyncio.current_task()
        previous, self._start_task = self._start_task, current
        try:
            if previous is not None and previous is not current and not previous.done():
                previous.cancel()
                await asyncio.gather(previous, return_exceptions=True)
            if self._start_task is not current:
                raise asyncio.CancelledError
            return await method(self, *args, **kwargs)
        finally:
            if self._start_task is current:
                self._start_task = None
    return run
