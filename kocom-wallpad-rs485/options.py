"""HA Add-on options reader.

/data/options.json (HA Supervisor가 자동 생성)을 읽어
기존 configparser.ConfigParser와 동일한 get(section, key, fallback) 인터페이스를 제공한다.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from typing import Any

log = logging.getLogger(__name__)

OPTIONS_FILE = '/data/options.json'

# Supervisor 서비스 API 엔드포인트. config.json에 services: ["mqtt:want"]가
# 선언되어 있으면 Supervisor가 이 URL로 Mosquitto 브로커의 접속 정보
# (host, port, username, password, ssl)를 제공한다.
SUPERVISOR_MQTT_URL = 'http://supervisor/services/mqtt'


def _fetch_supervisor_mqtt() -> dict | None:
    """Supervisor 서비스 API에서 MQTT 브로커 접속 정보를 조회한다.

    Supervisor는 애드온 컨테이너에 SUPERVISOR_TOKEN 환경변수를 주입하며,
    이 토큰으로 /services/mqtt를 조회하면 Mosquitto 애드온이 발급한
    전용 계정 정보를 받을 수 있다 (사용자가 직접 입력할 필요 없음).

    Mosquitto 애드온이 설치·실행 중이 아니거나, 애드온 밖에서 실행 중이라
    토큰이 없는 경우 None을 반환한다 (호출 측에서 폴백 처리).
    """
    token = os.environ.get('SUPERVISOR_TOKEN')
    if not token:
        return None
    req = urllib.request.Request(
        SUPERVISOR_MQTT_URL,
        headers={'Authorization': f'Bearer {token}'},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.load(resp)
    except Exception as e:
        log.warning('[MQTT] Supervisor 서비스 API 조회 실패: %r', e)
        return None
    # 응답 형식: {"result": "ok", "data": {"host": ..., "port": ..., ...}}
    return body.get('data') or None

# (section, key) → options.json 키 매핑
_MAP: dict[tuple[str, str], str] = {
    ('RS485',    'type'):                 'type',
    ('RS485',    'serial_port'):          'serial_port',
    ('RS485',    'socket_server'):        'socket_server',
    ('RS485',    'socket_port'):          'socket_port',
    ('MQTT',     'mqtt_server'):          'mqtt_server',
    ('MQTT',     'mqtt_port'):            'mqtt_port',
    ('MQTT',     'mqtt_allow_anonymous'): 'mqtt_allow_anonymous',
    ('MQTT',     'mqtt_username'):        'mqtt_username',
    ('MQTT',     'mqtt_password'):        'mqtt_password',
    ('Elevator', 'type'):                 'elevator_type',
    ('Elevator', 'rs485_floor'):          'rs485_floor',
    ('Elevator', 'tcpip_apt_server'):     'tcpip_apt_server',
    ('Elevator', 'tcpip_apt_port'):       'tcpip_apt_port',
    ('Elevator', 'tcpip_packet1'):        'tcpip_packet1',
    ('Elevator', 'tcpip_packet2'):        'tcpip_packet2',
    ('Elevator', 'tcpip_packet3'):        'tcpip_packet3',
    ('Elevator', 'tcpip_packet4'):        'tcpip_packet4',
    ('Log',      'show_recv_hex'):        'log_recv_hex',
    ('Log',      'show_query_hex'):       'log_recv_hex',   # 동일 키로 통합
    ('Log',      'show_mqtt_publish'):    'log_mqtt_publish',
    ('User',     'init_temp'):            'init_temp',
    ('Away',     'mode'):                 'away_mode',
    ('Away',     'gas_room'):             'away_gas_room',
}


class Options:
    """
    /data/options.json을 읽어 configparser 호환 인터페이스를 제공한다.

    기존 코드의 config.get(section, key, fallback=...) 호출을 그대로 유지할 수 있다.
    """

    def __init__(self, path: str = OPTIONS_FILE) -> None:
        with open(path) as f:
            self._data: dict[str, Any] = json.load(f)

    def get_devices(self) -> list[dict]:
        """devices 리스트를 반환한다. 각 항목은 type, room(optional), count(optional) 키를 가진다."""
        return self._data.get('devices', [])

    def get_switch_count(self, dev_type: str, room: str) -> int:
        """devices 목록에서 (dev_type, room) 조합의 등장 횟수를 반환한다."""
        return sum(
            1 for d in self.get_devices()
            if d.get('type') == dev_type and d.get('room', 'livingroom') == room
        ) or 1  # 미설정 장치 패킷 수신 시 기본값 1

    def get_mqtt(self) -> dict:
        """MQTT 브로커 접속 정보를 결정해 dict로 반환한다.

        우선순위:
          1. 수동 설정 — 사용자가 mqtt_server를 직접 입력한 경우 그대로 사용
             (외부 브로커, HA 사용자 계정 등 모든 수동 구성 지원)
          2. 자동 발견 — mqtt_server가 비어 있으면 Supervisor 서비스 API로
             Mosquitto 애드온의 접속 정보를 받아 사용 (권장, 설정 불필요)
          3. 폴백 — 둘 다 불가하면 기존 기본값(172.30.32.1:1883, 익명)

        반환 dict 키:
          server, port, username, password, ssl(bool), source(로그용 출처)
        """
        server = self._data.get('mqtt_server') or ''

        # 1. 수동 설정이 있으면 최우선
        if server:
            anon = bool(self._data.get('mqtt_allow_anonymous'))
            return {
                'server':   server,
                'port':     int(self._data.get('mqtt_port') or 1883),
                'username': None if anon else (self._data.get('mqtt_username') or None),
                'password': None if anon else (self._data.get('mqtt_password') or None),
                'ssl':      False,
                'source':   'manual',
            }

        # 2. Supervisor 서비스 API 자동 발견
        svc = _fetch_supervisor_mqtt()
        if svc:
            return {
                'server':   svc.get('host') or '172.30.32.1',
                'port':     int(svc.get('port') or 1883),
                'username': svc.get('username') or None,
                'password': svc.get('password') or None,
                'ssl':      bool(svc.get('ssl')),
                'source':   'supervisor',
            }

        # 3. 폴백: 이전 버전과 동일한 기본값 (Mosquitto 내부 주소, 익명 접속)
        log.warning('[MQTT] 수동 설정도 자동 발견도 없어 기본값으로 접속을 시도합니다.')
        return {
            'server':   '172.30.32.1',
            'port':     1883,
            'username': None,
            'password': None,
            'ssl':      False,
            'source':   'fallback',
        }

    def get(self, section: str, key: str, fallback: Any = None) -> str:
        opt_key = _MAP.get((section, key))
        if opt_key is None:
            return str(fallback) if fallback is not None else ''

        val = self._data.get(opt_key)
        if val is None:
            return str(fallback) if fallback is not None else ''

        # devices 리스트 → 콤마 구분 문자열 (split(',') 하는 기존 코드와 호환)
        if isinstance(val, list):
            return ', '.join(str(v) for v in val)

        # bool → 'True'/'False' 문자열 (== 'True' 비교하는 기존 코드와 호환)
        return str(val)
