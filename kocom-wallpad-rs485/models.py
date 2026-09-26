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
        """0x09 = broadcast, 0x0B = send, 0x0D = ack."""
        return (self.raw[3] >> 4) & 0x0F

    @property
    def seq(self) -> int:
        """byte3 하위 니블 — 재시도마다 0xC→0xD→0xE로 증가한다.

        ACK는 요청과 같은 값을 되돌려주므로 요청·응답 짝을 맞추는 데 쓸 수 있다.
        """
        return self.raw[3] & 0x0F

    @property
    def retry_count(self) -> int:
        """재전송 횟수 (0 = 최초 전송)."""
        return max(0, self.seq - 0x0C)

    @property
    def is_broadcast(self) -> bool:
        """방 코드 0xFF 대상 브로드캐스트. ACK도 재시도도 없다."""
        return self.packet_type == 0x09

    @property
    def is_ack(self) -> bool:
        """직전 전송 프레임의 반향. 페이로드는 상태가 아니다."""
        return self.packet_type == 0x0D

    @property
    def from_wallpad(self) -> bool:
        return self.src[0] == 0x01

    @property
    def is_state_report(self) -> bool:
        """장치가 스스로 보낸 상태 보고인가.

        실측 결과 신뢰할 수 있는 상태는 **장치 발신 0x0B 프레임**뿐이다.
        - 월패드 발신 0x0B: 명령·조회이며 '목표' 값이다 (실제 상태가 아님)
        - 0x0D(ACK): 직전 프레임의 반향이므로 명령 페이로드가 그대로 담긴다
          (주방 콘센트처럼 장치가 명령을 일부 거부하면 ACK와 실제가 다르다)
        조회에 대한 응답도 장치가 별도의 0x0B 보고를 이어 보내므로 누락되지 않는다.
        """
        return not self.from_wallpad and self.packet_type == 0x0B

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
