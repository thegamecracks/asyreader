import asyncio
import concurrent.futures
import contextlib
import queue
import threading
from typing import (
    Any,
    Callable,
    Generic,
    NamedTuple,
    Protocol,
    Self,
    TypeVar,
    runtime_checkable,
)

T_co = TypeVar("T_co", covariant=True)


@runtime_checkable
class Readable(Protocol, Generic[T_co]):
    def close(self) -> Any:
        ...

    def read(self, size: int | None = -1, /) -> T_co:
        ...


class _ReaderItem(NamedTuple, Generic[T_co]):
    fut: concurrent.futures.Future[T_co]
    size: int | None


class AsyncReader(Generic[T_co]):
    """Allows reading from a file in a different thread.

    >>> reader = AsyncReader(open("file.txt"))
    >>> await reader.start()
    >>> await reader.read(32)
    'Hello world!\nfoobar2000\nspam and'
    >>> await reader.close()

    In context manager style:

    >>> async with AsyncReader(open("file.txt")) as reader:
    ...     await reader.read(32)
    'Hello world!\nfoobar2000\nspam and'

    A function can also be given to allow opening the reader more than once:

    >>> reader = AsyncReader(lambda: open("file.txt"))
    >>> async with reader:
    ...     await reader.read(32)
    'Hello world!\nfoobar2000\nspam and'
    ...     await reader.read()
    ' ham\n'
    >>> async with reader:
    ...     await reader.read(12)
    'Hello world!'

    """

    _read_queue: queue.Queue[_ReaderItem[T_co] | None]
    """A queue of read requests to be resolved."""
    _thread: threading.Thread | None
    """The thread to handle reading from the file."""
    _file: Readable | None
    """The file being read by the thread.

    This file should be closed once the thread is done."""
    _task: asyncio.Task | None
    """The current task if opened by a context manager.

    If an error occurred in the thread, this task should be cancelled
    so the context manager can propagate the exception.

    """
    _start_fut: concurrent.futures.Future[None] | None
    """A future to signal when the thread has started.

    If an error occurs during startup, the exception should be set here.

    """
    _is_closing: bool
    """Indicates when the reader has started closing."""
    _close_fut: concurrent.futures.Future[None] | None
    """A future to signal when the thread has closed.

    If an error occurred in the thread, the exception should be set here.

    """

    def __init__(self, opener: Readable[T_co] | Callable[[], Readable[T_co]]):
        self.opener = opener

        self._read_queue = queue.Queue()
        self._thread = None
        self._file = None
        self._start_fut = None
        self._is_closing = False
        self._close_fut = None

    async def __aenter__(self) -> Self:
        self._task = asyncio.current_task()
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, tb) -> None:
        try:
            await self.close()
        finally:
            self._task = None

    async def start(self) -> None:
        """Start and wait for the reader to be ready.

        If a function was given to open the file, any exceptions raised
        by it will be propagated here.

        :raises RuntimeError: The reader is already running.

        """
        if self._thread is not None and self._thread.is_alive():
            raise RuntimeError("Reader is already running")

        self._thread = threading.Thread(target=self._read_in_background)
        self._file = None
        self._start_fut = concurrent.futures.Future()
        self._is_closing = False
        self._close_fut = concurrent.futures.Future()
        self._close_fut.add_done_callback(self._close_callback)

        self._thread.start()
        await asyncio.wrap_future(self._start_fut)

    async def read(self, size: int | None = -1) -> T_co:
        """Read size characters from the file.

        Any :exc:`Exception` raised during the read will be propagated here.
        If :exc:`BaseException` gets raised, the reader will also be closed.

        :raises ValueError:
            The reader has been closed or is in the process of closing.

        """
        if self._thread is None or self._close_fut is None:
            raise ValueError("Reader thread has not started")
        elif self._close_fut.done():
            raise ValueError("Reader thread has closed")

        fut = concurrent.futures.Future[T_co]()
        self._read_queue.put_nowait(_ReaderItem(fut, size))
        return await asyncio.wrap_future(fut)

    async def close(self) -> None:
        """Close the current file and wait for the reader to stop.

        If :exc:`BaseException` was raised in the reader thread,
        that exception will be propagated here.

        This method is idempotent.

        """
        if self._close_fut is None:
            return
        elif self._is_closing:
            return await asyncio.wrap_future(self._close_fut)

        self._is_closing = True
        self._cancel_queue()
        self._read_queue.put_nowait(None)

        # In case the thread is currently reading, try closing the
        # file to interrupt it
        assert self._start_fut is not None
        await asyncio.wrap_future(self._start_fut)
        assert self._file is not None
        self._file.close()

        return await asyncio.wrap_future(self._close_fut)

    def _read_in_background(self) -> None:
        try:
            with contextlib.closing(self._open_file()) as self._file:
                self._set_started(None)
                self._read_loop()
        except BaseException as e:
            # If an error occurred while opening the file, we should send it
            # to whoever's starting the reader.
            self._set_started(e)
            self._set_closed(e)
        else:
            self._set_closed(None)

    def _open_file(self) -> Readable[T_co]:
        if not isinstance(self.opener, Readable):
            return self.opener()
        return self.opener

    def _set_started(self, exc: BaseException | None) -> None:
        assert self._start_fut is not None

        if self._start_fut.done():
            pass
        elif exc is None:
            self._start_fut.set_result(None)
        else:
            self._start_fut.set_exception(exc)

    def _read_loop(self) -> None:
        assert self._file is not None
        while True:
            item = self._read_queue.get()
            if item is None:
                break

            try:
                data = self._file.read(item.size)
            except Exception as e:
                if not item.fut.done():
                    item.fut.set_exception(e)
            except BaseException as e:
                if not item.fut.done():
                    item.fut.set_exception(e)
                raise
            else:
                if not item.fut.done():
                    item.fut.set_result(data)
            finally:
                self._read_queue.task_done()

    def _set_closed(self, exc: BaseException | None) -> None:
        self._is_closing = True
        assert self._close_fut is not None

        if self._close_fut.done():
            pass
        elif exc is None:
            self._close_fut.set_result(None)
        else:
            self._close_fut.set_exception(exc)

    def _close_callback(self, fut: concurrent.futures.Future) -> None:
        if fut.exception() and self._task is not None:
            # Interrupt the current task so close() can propagate the exception
            self._task.cancel()

    def _cancel_queue(self) -> None:
        with contextlib.suppress(queue.Empty):
            while True:
                item = self._read_queue.get_nowait()
                if item is None:
                    continue
                elif not item.fut.done():
                    item.fut.set_exception(ValueError("Reader is being closed"))
