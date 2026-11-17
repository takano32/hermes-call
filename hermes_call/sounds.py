"""Local, quiet status earcons; never synthesize or send private text to a provider."""

import math
import struct
from collections import Counter
from enum import StrEnum
from functools import lru_cache

from .audio import RATE, AudioBuffer


class Stage(StrEnum):
    LISTENING = "listening"
    RECEIVED = "received"
    TRANSCRIBING = "transcribing"
    THINKING = "thinking"
    TOOL_RUNNING = "tool_running"
    TOOL_DONE = "tool_done"
    SYNTHESIZING = "synthesizing"
    ERROR = "error"


# (frequency Hz, seconds). Zero frequency denotes a gap. All tones have a fade envelope.
PATTERNS = {
    Stage.LISTENING: ((660, 0.07), (0, 0.04), (880, 0.07)),
    Stage.RECEIVED: ((880, 0.09), (1175, 0.09)),
    Stage.TRANSCRIBING: ((440, 0.06), (0, 0.06), (440, 0.06)),
    Stage.THINKING: ((520, 0.12),),
    Stage.TOOL_RUNNING: ((740, 0.06), (0, 0.06), (550, 0.06)),
    Stage.TOOL_DONE: ((660, 0.06), (990, 0.08)),
    Stage.SYNTHESIZING: ((1040, 0.05), (0, 0.05), (1040, 0.05)),
    Stage.ERROR: ((440, 0.12), (0, 0.06), (330, 0.16)),
}


@lru_cache(maxsize=128)
def earcon(stage: Stage, volume: float) -> bytes:
    samples = bytearray()
    for frequency, seconds in PATTERNS[stage]:
        length = round(RATE * seconds)
        fade = max(1, round(RATE * 0.008))
        for index in range(length):
            envelope = min(1.0, index / fade, (length - 1 - index) / fade)
            sample = round(
                32767 * volume * envelope * math.sin(2 * math.pi * frequency * index / RATE)
            )
            samples.extend(struct.pack("<h", sample if frequency else 0))
    return bytes(samples)


class StatusSounds:
    def __init__(self, audio: AudioBuffer, enabled: bool, volume: float):
        self.audio, self.enabled, self.volume = audio, enabled, volume
        self.tools: Counter[str] = Counter()

    def clear(self) -> None:
        self.audio.set_effect(b"")

    def waiting(self, stage: Stage, *, cue: Stage | None = None) -> None:
        if not self.enabled:
            return
        pulse = earcon(stage, self.volume)
        # Sparse cues: most of each cycle is silent; no busy music under speech.
        loop = b"\0" * (RATE * 2 * 2) + pulse
        intro = earcon(cue, self.volume) + b"\0" * (RATE // 10 * 2) if cue else b""
        self.audio.set_effect(intro + pulse, loop)

    async def listening(self) -> None:
        self.clear()
        self.tools.clear()
        if self.enabled:
            await self.audio.play(earcon(Stage.LISTENING, self.volume))

    def thinking(self) -> None:
        self.tools.clear()
        self.waiting(Stage.THINKING)

    def tool_event(self, event: dict) -> None:
        kind = event["type"]
        key = event.get("tool_call_id") or event.get("name") or "unknown"
        if kind == "tool_use":
            self.tools[key] += 1
            self.waiting(Stage.TOOL_RUNNING)
        elif kind == "tool_result":
            if self.tools[key] > 0:
                self.tools[key] -= 1
            stage = Stage.TOOL_RUNNING if any(self.tools.values()) else Stage.THINKING
            self.waiting(stage, cue=Stage.ERROR if event.get("is_error") else Stage.TOOL_DONE)

    async def error(self) -> None:
        self.clear()
        if self.enabled:
            await self.audio.play(earcon(Stage.ERROR, self.volume))
