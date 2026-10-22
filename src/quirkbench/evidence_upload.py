"""Whole-file HTTPS upload with bounded network stalls, not bounded file size."""

import asyncio

import aiohttp

from .files import disk


class EvidenceFile(aiohttp.payload.Payload):
    def __init__(self, stream, *, idle_seconds=120):
        super().__init__(stream, content_type="application/octet-stream")
        self.idle_seconds = idle_seconds
        self._size = __import__("os").fstat(stream.fileno()).st_size

    def decode(self, encoding="utf-8", errors="strict"):
        raise TypeError("Evidence is streamed as binary bytes.")

    async def write(self, writer):
        # aiohttp may retry an idempotent PUT after a lost keepalive connection.
        await disk(self._value.seek, 0)
        while chunk := await disk(self._value.read, 256 * 1024):
            # aiohttp's writer applies transport backpressure here. A large file
            # can take hours; only a stalled individual network write expires.
            await asyncio.wait_for(writer.write(chunk), self.idle_seconds)


UPLOAD_TIMEOUT = aiohttp.ClientTimeout(total=None, connect=30, sock_read=120)
