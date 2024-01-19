import asyncio
import contextlib
import io
from typing import NoReturn

import pytest

from asyreader import AsyncReader, Readable


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content,chunk_size",
    [
        ("Hello world!\nfoobar2000\nspam and ham\n", 16),
        (b"Hello world!\nfoobar2000\nspam and ham\n", 16),
    ],
)
async def test_in_memory(content: bytes | str, chunk_size: int):
    start_chunks = range(0, len(content) + chunk_size, chunk_size)
    expected = [content[i : i + chunk_size] for i in start_chunks]
    read = []

    if isinstance(content, str):
        file = io.StringIO(content)
    else:
        file = io.BytesIO(content)

    async with AsyncReader(file) as reader:
        for _ in start_chunks:
            read.append(await reader.read(chunk_size))

    assert read == expected


class BaseExceptionTest(BaseException):
    ...


class ExceptionTest(Exception):
    ...


def raise_exception_test():
    raise ExceptionTest()


class ExceptionReader(Readable):
    def __init__(self, exc: BaseException) -> None:
        self.exc = exc

    def close(self) -> None:
        ...

    def read(self) -> NoReturn:
        raise self.exc


@pytest.mark.asyncio
async def test_exception_on_open():
    reader = AsyncReader(raise_exception_test)

    with pytest.raises(ExceptionTest):
        await reader.start()

    with pytest.raises(ExceptionTest):
        await reader.close()

    with pytest.raises(ExceptionTest):
        async with reader:
            ...


@pytest.mark.asyncio
async def test_exception_on_read():
    readable = ExceptionReader(ExceptionTest())
    async with AsyncReader(readable) as reader:
        with pytest.raises(ExceptionTest):
            await reader.read()


@pytest.mark.asyncio
async def test_base_exception_on_read():
    readable = ExceptionReader(BaseExceptionTest())
    reader = AsyncReader(readable)

    await reader.start()
    with pytest.raises(BaseExceptionTest):
        await reader.read()
    await reader.close()

    with pytest.raises(BaseExceptionTest):
        async with reader:
            await reader.read()
