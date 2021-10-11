import asyncio
import sys
import typing as t
from concurrent.futures import Executor
from weakref import WeakSet

from aiomisc_log import LogLevel
from .context import Context, get_context
from .log import LogFormat, basic_config
from .service import Service
from .signal import Signal
from .utils import cancel_tasks, create_default_event_loop, event_loop_policy


ExecutorType = Executor


if sys.version_info < (3, 7):
    asyncio_all_tasks = asyncio.Task.all_tasks
    asyncio_current_task = asyncio.Task.current_task
else:
    asyncio_all_tasks = asyncio.all_tasks
    asyncio_current_task = asyncio.current_task


class Entrypoint:

    PRE_START = Signal()
    POST_STOP = Signal()

    async def _start(self) -> None:
        if self.log_config:
            basic_config(
                level=self.log_level,
                log_format=self.log_format,
                buffered=True,
                loop=self.loop,
                buffer_size=self.log_buffer_size,
                flush_interval=self.log_flush_interval,
            )

        for signal in (self.pre_start, self.post_stop):
            signal.freeze()

        await self.pre_start.call(entrypoint=self, services=self.services)

        await asyncio.gather(
            *[self._start_service(svc) for svc in self.services],
        )

    def __init__(
        self, *services: Service, loop: asyncio.AbstractEventLoop = None,
        pool_size: int = None,
        log_level: t.Union[int, str] = LogLevel.default(),
        log_format: t.Union[str, LogFormat] = LogFormat.default(),
        log_buffer_size: int = 1024,
        log_flush_interval: float = 0.2,
        log_config: bool = True,
        policy: asyncio.AbstractEventLoopPolicy = event_loop_policy,
        debug: bool = False
    ):

        """

        :param debug: set debug to event loop
        :param loop: loop
        :param services: Service instances which will be starting.
        :param pool_size: thread pool size
        :param log_level: Logging level which will be configured
        :param log_format: Logging format which will be configures
        :param log_buffer_size: Buffer size for logging
        :param log_flush_interval: interval in seconds for flushing logs
        :param log_config: if False do not configure logging
        """

        self._debug = debug
        self._loop = loop
        self._loop_owner = False
        self._tasks: t.MutableSet[asyncio.Task] = WeakSet()
        self._thread_pool: t.Optional[ExecutorType] = None
        self._closing: t.Optional[asyncio.Event] = None

        self.ctx: t.Optional[Context] = None
        self.log_buffer_size = log_buffer_size
        self.log_config = log_config
        self.log_flush_interval = log_flush_interval
        self.log_format = log_format
        self.log_level = log_level
        self.policy = policy
        self.pool_size = pool_size
        self.services = services
        self.shutting_down = False
        self.pre_start = self.PRE_START.copy()
        self.post_stop = self.POST_STOP.copy()

        if self.log_config:
            basic_config(
                level=self.log_level,
                log_format=self.log_format,
                buffered=False,
            )

    async def closing(self) -> None:
        # Lazy initialization because event loop might be not exists
        if self._closing is None:
            self._closing = asyncio.Event()

        await self._closing.wait()

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None:
            self._loop, self._thread_pool = create_default_event_loop(
                pool_size=self.pool_size,
                policy=self.policy,
                debug=self._debug,
            )
            self._loop_owner = True

        return self._loop

    def __del__(self) -> None:
        if self._loop and self._loop.is_closed():
            return

        if self._loop_owner and self._loop is not None:
            self._loop.close()

    def __enter__(self) -> asyncio.AbstractEventLoop:
        self.loop.run_until_complete(self.__aenter__())
        return self.loop

    def __exit__(
        self, exc_type: t.Any, exc_val: t.Any, exc_tb: t.Any,
    ) -> None:
        if self.loop.is_closed():
            return

        if self.log_config:
            basic_config(
                level=self.log_level,
                log_format=self.log_format,
                buffered=False,
            )

        self.loop.run_until_complete(
            self.__aexit__(exc_type, exc_val, exc_tb),
        )

        if self._loop_owner and self._loop is not None:
            self._loop.close()

    async def __aenter__(self) -> "Entrypoint":
        if self._loop is None:
            # When __aenter__ called without __enter__
            self._loop = asyncio.get_event_loop()

        self.ctx = Context(loop=self.loop)
        await self._start()
        return self

    async def __aexit__(
        self, exc_type: t.Any, exc_val: t.Any, exc_tb: t.Any,
    ) -> None:
        try:
            if self.loop.is_closed():
                return

            await self.graceful_shutdown(exc_val)
            self.shutting_down = True
        finally:
            if self.ctx:
                self.ctx.close()

            if self._thread_pool:
                self._thread_pool.shutdown()

    async def _start_service(
        self, svc: Service,
    ) -> None:
        svc.set_loop(self.loop)

        start_task, ev_task = map(
            asyncio.ensure_future, (
                svc.start(), svc.start_event.wait(),
            ),
        )

        await asyncio.wait(
            (start_task, ev_task),
            return_when=asyncio.FIRST_COMPLETED,
        )

        self.loop.call_soon(svc.start_event.set)
        await ev_task

        if start_task.done():
            # raise an Exception when failed
            await start_task
            return
        else:
            self._tasks.add(start_task)

        return None

    async def _cancel_background_tasks(self) -> None:
        tasks = asyncio_all_tasks(self._loop)
        current_task = asyncio_current_task(self.loop)
        await cancel_tasks(task for task in tasks if task is not current_task)

    async def graceful_shutdown(self, exception: Exception) -> None:
        if self._closing:
            self._closing.set()

        tasks = [
            asyncio.shield(svc.stop(exception)) for svc in self.services
        ]

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        await cancel_tasks(set(self._tasks))

        if self._loop_owner:
            await self._cancel_background_tasks()

        await self.post_stop.call(entrypoint=self)
        await self.loop.shutdown_asyncgens()


entrypoint = Entrypoint

T = t.TypeVar("T")


def run(
    coro: t.Coroutine[None, t.Any, T],
    *services: Service,
    **kwargs: t.Any
) -> T:
    with entrypoint(*services, **kwargs) as loop:
        return loop.run_until_complete(coro)


__all__ = ("entrypoint", "Entrypoint", "get_context", "run")
