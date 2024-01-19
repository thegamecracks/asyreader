import io

import pytest

from asyreader import AsyncReader


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
