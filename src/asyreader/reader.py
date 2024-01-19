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

T = TypeVar("T")
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
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, tb) -> None:
        await self.close()

    async def start(self) -> None:
        """Start and wait for the reader to be ready.

        If a function was given to open the file, any exceptions raised
        by it will be propagated here.

        :raises RuntimeError: The reader is already running.

        """
        if self._close_fut is not None and not self._close_fut.done():
            # Technically this can start a second thread while the last
            # one is still alive, but it shouldn't be doing anything
            # after this is set.
            raise RuntimeError("Reader is already running")

        self._thread = threading.Thread(target=self._read_in_background)
        self._file = None
        self._start_fut = concurrent.futures.Future()
        self._is_closing = False
        self._close_fut = concurrent.futures.Future()

        self._thread.start()
        await self._wrap_future(self._start_fut)

    async def read(self, size: int | None = -1) -> T_co:
        """Read size characters from the file.

        Any exception raised during the read will be propagated here.
        Keep in mind that exceptions do not close the reader so using
        structured concurrency is recommended to promptly close the reader
        after an exception, for example::

            async with AsyncReader(readable) as reader, asyncio.TaskGroup() as tg:
                tg.create_task(do_some_reads(reader))
                tg.create_task(do_some_other_reads(reader))

        :raises ValueError:
            The reader has been closed or is in the process of closing.

        """
        if self._thread is None or self._close_fut is None:
            raise ValueError("Reader thread has not started")
        elif self._close_fut.done():
            raise ValueError("Reader thread has closed")

        fut = concurrent.futures.Future[T_co]()
        self._read_queue.put_nowait(_ReaderItem(fut, size))
        return await self._wrap_future(fut)

    async def close(self) -> None:
        """Close the current file and wait for the reader to stop.

        If a function was given to open the file, any exceptions raised
        by it will be propagated here.

        This method is idempotent.

        """
        if self._close_fut is None:
            return
        elif self._is_closing:
            return await self._wrap_future(self._close_fut)

        self._is_closing = True
        self._cancel_queue()
        self._read_queue.put_nowait(None)

        # In case the thread is currently reading, try closing the
        # file to interrupt it
        assert self._start_fut is not None
        await self._wrap_future(self._start_fut)
        assert self._file is not None
        self._file.close()

        return await self._wrap_future(self._close_fut)

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
            except BaseException as e:
                # This exception will be the user's responsibility to deal with
                if not item.fut.done():
                    item.fut.set_exception(e)
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

    def _wrap_future(self, fut: concurrent.futures.Future[T]) -> asyncio.Future[T]:
        # The thread doesn't need to know of our cancellations
        # since close() is sufficient to interrupt it
        return asyncio.shield(asyncio.wrap_future(fut))

    def _cancel_queue(self) -> None:
        with contextlib.suppress(queue.Empty):
            while True:
                item = self._read_queue.get_nowait()
                if item is not None and not item.fut.done():
                    item.fut.set_exception(ValueError("Reader is being closed"))
                self._read_queue.task_done()
