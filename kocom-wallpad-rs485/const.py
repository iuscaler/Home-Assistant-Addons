"""Constants and device maps for Kocom Wallpad RS485 bridge."""

SW_VERSION = '2026.05.30'
CONFIG_FILE = '/share/kocom/kocom.conf'

# ── RS485 패킷 구조 ──────────────────────────────────────────────
PACKET_PREFIX    = bytes([0xAA, 0x55])
PACKET_SUFFIX    = bytes([0x0D, 0x0D])
PACKET_LEN       = 21

# ── 타이밍 ──────────────────────────────────────────────────────
IDLE_GAP         = 0.03   # 송신 전 버스 유휴 보장 시간 (초)
SEND_RETRY       = 4      # 최대 재전송 횟수
SEND_RETRY_GAP   = 0.30   # 재전송 간격 (초)
POLLING_INTERVAL = 300    # 장치 상태 폴링 주기 (초)

# ── 장치 코드 ────────────────────────────────────────────────────
DEVICE_CODE: dict[int, str] = {
    0x0E: 'light',
    0x3B: 'outlet',
    0x36: 'thermo',
    0x39: 'aircon',
    0x48: 'fan',
    0x2C: 'gas',
    0x44: 'elevator',
    0x60: 'motion',
    0x98: 'airquality',
}
CODE_DEVICE: dict[str, int] = {v: k for k, v in DEVICE_CODE.items()}

# ── 방 이름 ──────────────────────────────────────────────────────
ROOM_NAME: dict[int, str] = {
    0x00: 'livingroom',
    0x01: 'room1',
    0x02: 'room2',
    0x03: 'room3',
    0x04: 'kitchen',
}
ROOM_CODE: dict[str, int] = {v: k for k, v in ROOM_NAME.items()}

# ── 에어컨 모드 ──────────────────────────────────────────────────
AIRCON_HVAC_CODE: dict[str, int] = {
    'cool': 0x00, 'fan_only': 0x01, 'dry': 0x02, 'auto': 0x03,
}
AIRCON_HVAC_NAME: dict[int, str] = {v: k for k, v in AIRCON_HVAC_CODE.items()}

AIRCON_FAN_CODE: dict[str, int] = {
    'low': 0x01, 'medium': 0x02, 'high': 0x03, 'auto': 0x04,
}
AIRCON_FAN_NAME: dict[int, str] = {v: k for k, v in AIRCON_FAN_CODE.items()}

# ── 환기장치 프리셋 ──────────────────────────────────────────────
VENT_PRESET_NAME: dict[int, str] = {
    0x00: 'unknown',
    0x01: 'ventilation',
    0x02: 'auto',
    0x03: 'bypass',
    0x05: 'sleep',
    0x08: 'air purification',
}
VENT_PRESET_CODE: dict[str, int] = {v: k for k, v in VENT_PRESET_NAME.items()}

# ── 엘리베이터 방향 ──────────────────────────────────────────────
ELEVATOR_DIR: dict[int, str] = {
    0x00: 'idle',
    0x01: 'downward',
    0x02: 'upward',
    0x03: 'arrival',
}

# ── 폴링 제외 장치 ───────────────────────────────────────────────
NO_POLL_DEVICES = frozenset({'wallpad', 'elevator', 'motion', 'airquality', 'lightcutoff'})
