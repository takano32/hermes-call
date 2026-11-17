"""Bounded PCM exchange between the native media clock and asyncio conversation loop."""

import asyncio
import struct
import threading
import time
from collections import deque

RATE = 16000
BYTES_PER_SECOND = RATE * 2


class AudioBuffer:
    def __init__(self):
        self.lock = threading.Lock()
        self.incoming: deque[bytes] = deque()
        self.incoming_size = 0
        self.capture = False
        self.overflow = False
        self.outgoing = b""
        self.position = 0
        self.ended = threading.Event()

    def receive(self, pcm: bytes) -> None:
        with self.lock:
            if self.capture:
                if self.incoming_size + len(pcm) > BYTES_PER_SECOND * 2:
                    self.overflow = True
                    return
                self.incoming.append(pcm)
                self.incoming_size += len(pcm)

    def provide(self, size: int) -> bytes:
        with self.lock:
            chunk = self.outgoing[self.position : self.position + size]
            self.position += len(chunk)
            return chunk.ljust(size, b"\0")

    def stop(self) -> None:
        self.ended.set()
        with self.lock:
            self.capture = False
            self.outgoing = b""
            self.position = 0

    async def play(self, pcm: bytes) -> None:
        if len(pcm) > BYTES_PER_SECOND * 180 or len(pcm) % 2:
            raise ValueError("playback must be bounded PCM16")
        with self.lock:
            self.capture = False
            self.outgoing, self.position = pcm, 0
        try:
            async with asyncio.timeout(185):
                while not self.ended.is_set():
                    with self.lock:
                        finished = self.position >= len(self.outgoing)
                    if finished:
                        # Allow the final native frame to reach the RTP sender.
                        await asyncio.sleep(0.06)
                        return
                    await asyncio.sleep(0.02)
                raise ConnectionError("call disconnected")
        finally:
            with self.lock:
                self.outgoing, self.position = b"", 0

    async def listen(
        self, limit_seconds: float = 30, silence_seconds: float = 2, wait_seconds: float = 10
    ) -> bytes:
        with self.lock:
            self.incoming.clear()
            self.incoming_size = 0
            self.overflow = False
            self.capture = True
        audio = bytearray()
        silence = 0
        started = time.monotonic()
        voiced = False
        try:
            while time.monotonic() - started < limit_seconds:
                if self.ended.is_set():
                    raise ConnectionError("call disconnected")
                with self.lock:
                    if self.overflow:
                        raise RuntimeError("audio consumer fell behind")
                    chunks = list(self.incoming)
                    self.incoming.clear()
                    self.incoming_size = 0
                for chunk in chunks:
                    if len(chunk) % 2:
                        raise RuntimeError("invalid PCM frame")
                    if len(audio) + len(chunk) > BYTES_PER_SECOND * limit_seconds:
                        return bytes(audio) if voiced else b""
                    audio.extend(chunk)
                    energy = sum(x[0] ** 2 for x in struct.iter_unpack("<h", chunk))
                    if chunk and energy / (len(chunk) // 2) >= 200**2:
                        voiced, silence = True, 0
                    else:
                        silence += len(chunk)
                    if voiced and silence >= BYTES_PER_SECOND * silence_seconds:
                        return bytes(audio)
                if not voiced and time.monotonic() - started >= wait_seconds:
                    return b""
                await asyncio.sleep(0.02)
            return bytes(audio) if voiced else b""
        finally:
            with self.lock:
                self.capture = False
                self.incoming.clear()
                self.incoming_size = 0
