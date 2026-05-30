"""RS485 packet frame model."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

from const import PACKET_PREFIX, PACKET_SUFFIX, PACKET_LEN, DEVICE_CODE


@dataclass(frozen=True)
class PacketFrame:
    """21-byte RS485 패킷의 구조화된 뷰."""

    raw: bytes

    # ── 유효성 검사 ─────────────────────────────────────────────
    @staticmethod
    def _checksum(buf: bytes) -> int:
        return sum(buf) % 256

    @property
    def is_valid(self) -> bool:
        if len(self.raw) != PACKET_LEN:
            return False
        if self.raw[:2] != PACKET_PREFIX or self.raw[-2:] != PACKET_SUFFIX:
            return False
        return self._checksum(self.raw[2:18]) == self.raw[18]

    # ── 패킷 필드 ────────────────────────────────────────────────
    @property
    def packet_type(self) -> int:
        """0x0B = send, 0x0D = ack."""
        return (self.raw[3] >> 4) & 0x0F

    @property
    def dest(self) -> bytes:
        return self.raw[5:7]

    @property
    def src(self) -> bytes:
        return self.raw[7:9]

    @property
    def command(self) -> int:
        return self.raw[9]

    @property
    def payload(self) -> bytes:
        return self.raw[10:18]

    @property
    def checksum(self) -> int:
        return self.raw[18]

    # ── 장치 식별 ────────────────────────────────────────────────
    @property
    def peer(self) -> Tuple[int, int]:
        """월패드(0x01)가 아닌 쪽의 (디바이스코드, 방코드)."""
        if self.dest[0] == 0x01:
            return (self.src[0], self.src[1])
        if self.src[0] == 0x01:
            return (self.dest[0], self.dest[1])
        return (0, 0)

    @property
    def dev_code(self) -> int:
        return self.peer[0]

    @property
    def dev_room(self) -> int:
        return self.peer[1]

    @property
    def dev_type(self) -> Optional[str]:
        return DEVICE_CODE.get(self.dev_code)
