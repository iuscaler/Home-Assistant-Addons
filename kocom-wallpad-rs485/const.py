"""Constants and device maps for Kocom Wallpad RS485 bridge."""

import json as _json


def _load_version() -> str:
    try:
        with open('/config.json') as f:
            return _json.load(f).get('version', 'unknown')
    except Exception:
        return 'unknown'


SW_VERSION: str = _load_version()

# ── RS485 패킷 구조 ──────────────────────────────────────────────
PACKET_PREFIX    = bytes([0xAA, 0x55])
PACKET_SUFFIX    = bytes([0x0D, 0x0D])
PACKET_LEN       = 21

# byte3 = 상위 니블(패킷 타입) + 하위 니블(재시도 시퀀스)
PT_BROADCAST     = 0x09   # 방 코드 0xFF 대상. ACK·재시도 없음
PT_SEND          = 0x0B   # 명령·조회·상태보고
PT_ACK           = 0x0D   # 직전 전송의 반향 (상태가 아님)
SEQ_FIRST        = 0x0C   # 최초 전송. 재시도는 0x0D, 0x0E

# ── 커맨드 ──────────────────────────────────────────────────────
CMD_STATE        = 0x00   # 상태 보고 / 제어
CMD_ON           = 0x01   # 열림(가스) / 엘리베이터 이벤트 / 시각(0x86)
CMD_OFF          = 0x02   # 차단(가스·0x2D) / 환기 미지원 명령
CMD_MOTION       = 0x04   # 동작감지 보고
CMD_QUERY        = 0x3A   # 상태 조회
CMD_TIME_SET     = 0x3B   # 시각 전달 (월패드 → 장치)
CMD_TIME_REQ     = 0x3C   # 시각 요청 (장치 → 월패드)
CMD_CUTOFF_ON    = 0x65   # 전체 소등
CMD_CUTOFF_OFF   = 0x66   # 전체 소등 해제 (소등 직전 상태 복원)

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
    # 0x60의 실제 기능은 미상이다. 실측에서 사람의 이동·월패드 조작·문 개폐와
    # 모두 무관했고, 페이로드는 on/off 두 값뿐이며 펄스 길이가 1.7초로 고정이다.
    # 슬러그 'motion'은 기존 사용자 설정(devices: type=motion)과 MQTT 토픽
    # 호환을 위해 유지한다. 표시 이름만 중립적으로 바꿨다.
    0x60: 'motion',
    0x98: 'airquality',
    0x2D: 'shutoff2',   # 정체 미상. 외출모드 진입 시 차단 명령만 받고 응답 없음
    0x86: 'timesync',   # 월패드가 시각을 보내는 주소. 응답 없음
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

# HA에 노출할 프리셋. 'air purification'(0x08)은 실측에서 관측되지 않아 제외한다.
VENT_PRESET_SUPPORTED: list[str] = ['ventilation', 'auto', 'bypass', 'sleep']

# ── 엘리베이터 방향 ──────────────────────────────────────────────
ELEVATOR_DIR: dict[int, str] = {
    0x00: 'idle',
    0x01: 'downward',
    0x02: 'upward',
    0x03: 'arrival',
}

# 실측: cmd 0x01의 payload[0]이 이벤트를 나타낸다. cmd 0x00은 미지원 프레임.
ELEVATOR_EVENT: dict[int, str] = {
    0x00: 'called',
    0x03: 'arrival',
}

# ── 폴링 제외 장치 ───────────────────────────────────────────────
NO_POLL_DEVICES = frozenset({
    'wallpad', 'elevator', 'motion', 'airquality', 'lightcutoff',
    'shutoff2', 'timesync', 'away',
})

# ── 진단 ────────────────────────────────────────────────────────
DIAG_INTERVAL      = 30    # 진단 상태 발행 주기 (초)
# 버스는 유휴 시 5분 넘게 조용할 수 있다(실측 최대 4.8분). 넉넉히 잡는다.
BUS_ALIVE_TIMEOUT  = 600   # 이 시간 동안 패킷이 없으면 통신 끊김으로 판정 (초)
# 월패드 시각과 시스템 시각의 허용 오차
TIME_DRIFT_WARN    = 120   # 초

# 외출모드 진입 매크로의 단계 간 간격 (초) — 실측 월패드 동작을 모사
AWAY_STEP_GAP      = 2.0
