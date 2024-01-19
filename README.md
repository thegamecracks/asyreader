# asyreader

[![](https://img.shields.io/github/actions/workflow/status/thegamecracks/asyreader/black.yml?style=flat-square&label=black)](https://black.readthedocs.io/en/stable/)
[![](https://img.shields.io/github/actions/workflow/status/thegamecracks/asyreader/pyright.yml?style=flat-square&label=pyright)](https://microsoft.github.io/pyright/#/)
[![](https://img.shields.io/github/actions/workflow/status/thegamecracks/asyreader/pytest.yml?style=flat-square&logo=pytest&label=tests)](https://docs.pytest.org/en/stable/)

```py
import asyncio
from asyreader import AsyncReader

async def main():
    async with AsyncReader(lambda: open("file.txt", "rb")) as reader:
        await reader.read(32)

asyncio.run(main())
```

This Python library provides an `AsyncReader` class to transparently handle
blocking reads in a separate thread, useful for libraries that don't offer
an async interface. Anything that has a `read()` method and a `close()`
method can be passed.

## Usage

With Python 3.11+ and Git, you can install this library with:

```py
pip install git+https://github.com/thegamecracks/asyreader
```

## License

This project can be used under the MIT License.
