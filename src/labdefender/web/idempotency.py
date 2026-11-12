import asyncio
import re
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from labdefender.services.errors import DomainError

KEY = re.compile(r"[A-Za-z0-9_-]{8,64}")
T = TypeVar("T")


class InvalidIdempotencyKey(DomainError):
    def __init__(self) -> None:
        super().__init__("Некорректный запрос. Обновите приложение и попробуйте снова.")


class IdempotencyCache:
    """Remembers successful results of create requests per user and `Idempotency-Key`.

    A repeated request — a double tap or a retry after a lost response — gets the same result
    and the action (and its notifications) does not run again. A concurrent duplicate waits
    for the first request. Failures are not remembered, so a retry runs the action anew.
    Kept in memory: after a restart the keys are forgotten, which matters only for seconds.
    """

    def __init__(
        self,
        *,
        ttl: float = 600.0,
        limit: int = 1000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl, self._limit, self._clock = ttl, limit, clock
        self._done: OrderedDict[tuple[int, str], tuple[float, Any]] = OrderedDict()
        self._running: dict[tuple[int, str], asyncio.Future] = {}

    def _expire(self) -> None:
        now = self._clock()
        while self._done and next(iter(self._done.values()))[0] <= now:
            self._done.popitem(last=False)

    async def run(self, user_id: int, key: str | None, action: Callable[[], Awaitable[T]]) -> T:
        if not key:
            return await action()
        if KEY.fullmatch(key) is None:
            raise InvalidIdempotencyKey()
        entry = (user_id, key)
        self._expire()
        if entry in self._done:
            return self._done[entry][1]
        running = self._running.get(entry)
        if running is not None:
            return await asyncio.shield(running)
        future = asyncio.get_running_loop().create_future()
        self._running[entry] = future
        try:
            result = await action()
        except asyncio.CancelledError:
            future.cancel()
            raise
        except BaseException as error:
            future.set_exception(error)
            future.exception()  # Retrieved here: no warning when nobody else waits.
            raise
        else:
            future.set_result(result)
            self._done[entry] = (self._clock() + self._ttl, result)
            while len(self._done) > self._limit:
                self._done.popitem(last=False)
            return result
        finally:
            self._running.pop(entry, None)
