"""HA Add-on options reader.

/data/options.json (HA Supervisor가 자동 생성)을 읽어
기존 configparser.ConfigParser와 동일한 get(section, key, fallback) 인터페이스를 제공한다.
"""

from __future__ import annotations

import json
from typing import Any

OPTIONS_FILE = '/data/options.json'

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
    ('User',     'init_fan_mode'):        'init_fan_mode',
    ('User',     'light_count'):          'light_count',
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
        """특정 방의 light/outlet 개수를 반환한다. 미설정 시 전역 light_count를 사용한다."""
        default = int(self._data.get('light_count', 2))
        for dev in self.get_devices():
            if dev.get('type') == dev_type and dev.get('room') == room:
                count = dev.get('count')
                return int(count) if count is not None else default
        return default

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
