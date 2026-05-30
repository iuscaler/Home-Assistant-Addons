"""Home Assistant MQTT discovery payload publisher."""

from __future__ import annotations

import json
import logging
from collections import Counter

import aiomqtt  # type: ignore

from const import SW_VERSION, ROOM_CODE

log = logging.getLogger(__name__)

_BASE_DEVICE = {
    'name': '코콤 스마트 월패드',
    'ids':  'kocom_smart_wallpad',
    'mf':   'KOCOM',
    'mdl':  '스마트 월패드',
    'sw':   SW_VERSION,
}

_ROOM_KO: dict[str, str] = {
    'livingroom': '거실',
    'room1':      '방1',
    'room2':      '방2',
    'room3':      '방3',
    'kitchen':    '주방',
}


def _rname(room: str) -> str:
    return _ROOM_KO.get(room, room)


async def publish_discovery(mqtt: aiomqtt.Client, config) -> None:
    """devices 목록을 순회하며 HA MQTT Discovery 설정 발행."""
    dev_list = config.get_devices()

    # (type, room) 조합의 총 등장 횟수 — 단일/복수 판별에 사용
    occ_total: Counter = Counter(
        (d.get('type'), d.get('room', 'livingroom')) for d in dev_list
    )
    # 순회 중 각 (type, room)의 현재 순번 추적
    occ_seq: dict[tuple, int] = {}

    async def pub(topic: str, payload: dict) -> None:
        await mqtt.publish(topic, json.dumps(payload), retain=True)

    for entry in dev_list:
        dev  = entry.get('type', '')
        room = entry.get('room', 'livingroom')
        rko  = _rname(room)
        key  = (dev, room)

        # 순번 계산: 복수면 1, 2, … / 단일이면 None
        total = occ_total[key]
        if total > 1:
            occ_seq[key] = occ_seq.get(key, 0) + 1
            n: int | None = occ_seq[key]
        else:
            n = None

        if dev == 'light':
            if n is None:
                await pub(f'homeassistant/light/kocom_{room}_light/config', {
                    'name':         f'{rko} 조명',
                    'cmd_t':        f'kocom/{room}/light/command',
                    'stat_t':       f'kocom/{room}/light/state',
                    'stat_val_tpl': '{{ value_json.light }}',
                    'pl_on': 'on', 'pl_off': 'off', 'qos': 0,
                    'uniq_id': f'kocom_wallpad_light_{room}',
                    'device':  _BASE_DEVICE,
                })
            else:
                await pub(f'homeassistant/light/kocom_{room}_light{n}/config', {
                    'name':         f'{rko} 조명 {n}',
                    'cmd_t':        f'kocom/{room}/light/{n}/command',
                    'stat_t':       f'kocom/{room}/light/state',
                    'stat_val_tpl': '{{ value_json.light_' + str(n) + ' }}',
                    'pl_on': 'on', 'pl_off': 'off', 'qos': 0,
                    'uniq_id': f'kocom_wallpad_light_{room}_{n}',
                    'device':  _BASE_DEVICE,
                })

        elif dev == 'outlet':
            if n is None:
                await pub(f'homeassistant/switch/kocom_{room}_outlet/config', {
                    'name':    f'{rko} 콘센트',
                    'cmd_t':   f'kocom/{room}/outlet/command',
                    'stat_t':  f'kocom/{room}/outlet/state',
                    'val_tpl': '{{ value_json.outlet }}',
                    'pl_on': 'on', 'pl_off': 'off',
                    'dev_cla': 'outlet', 'qos': 0,
                    'uniq_id': f'kocom_wallpad_outlet_{room}',
                    'device':  _BASE_DEVICE,
                })
            else:
                await pub(f'homeassistant/switch/kocom_{room}_outlet{n}/config', {
                    'name':    f'{rko} 콘센트 {n}',
                    'cmd_t':   f'kocom/{room}/outlet/{n}/command',
                    'stat_t':  f'kocom/{room}/outlet/state',
                    'val_tpl': '{{ value_json.outlet_' + str(n) + ' }}',
                    'pl_on': 'on', 'pl_off': 'off',
                    'dev_cla': 'outlet', 'qos': 0,
                    'uniq_id': f'kocom_wallpad_outlet_{room}_{n}',
                    'device':  _BASE_DEVICE,
                })

        elif dev == 'fan':
            await pub(f'homeassistant/fan/kocom_{room}_fan/config', {
                'name':            f'{rko} 환기장치',
                'cmd_t':           f'kocom/{room}/fan/command',
                'stat_t':          f'kocom/{room}/fan/state',
                'stat_val_tpl':    '{{ value_json.state }}',
                'pr_mode_stat_t':  f'kocom/{room}/fan/state',
                'pr_mode_val_tpl': '{{ value_json.preset }}',
                'pr_mode_cmd_t':   f'kocom/{room}/fan/set_preset_mode/command',
                'pr_mode_cmd_tpl': '{{ value }}',
                'pr_modes': ['ventilation', 'auto', 'bypass', 'sleep', 'air purification'],
                'pct_cmd_t':       f'kocom/{room}/fan/set_speed/command',
                'pct_stat_t':      f'kocom/{room}/fan/state',
                'pct_val_tpl':     '{{ {64: 1, 128: 2, 192: 3}.get(value_json.speed | int, 0) }}',
                'spd_rng_min': 1,
                'spd_rng_max': 3,
                'pl_on': 'on', 'pl_off': 'off', 'qos': 0,
                'uniq_id': f'kocom_wallpad_fan_{room}',
                'device':  _BASE_DEVICE,
            })
            await pub(f'homeassistant/sensor/kocom_{room}_fan_co2/config', {
                'name':         f'{rko} CO₂',
                'stat_t':       f'kocom/{room}/fan/co2',
                'val_tpl':      '{{ value_json.value }}',
                'dev_cla':      'carbon_dioxide',
                'unit_of_meas': 'ppm',
                'ic':           'mdi:molecule-co2',
                'qos': 0,
                'uniq_id': f'kocom_wallpad_fan_co2_{room}',
                'device':  _BASE_DEVICE,
            })
            await pub(f'homeassistant/sensor/kocom_{room}_fan_timer/config', {
                'name':         f'{rko} 환기장치 예약 끄기',
                'stat_t':       f'kocom/{room}/fan/state',
                'val_tpl':      '{{ value_json.timer }}',
                'unit_of_meas': 'h',
                'ic':           'mdi:timer-outline',
                'qos': 0,
                'uniq_id': f'kocom_wallpad_fan_timer_{room}',
                'device':  _BASE_DEVICE,
            })

        elif dev == 'thermo':
            idx = ROOM_CODE.get(room, 0)
            await pub(f'homeassistant/climate/kocom_{room}_thermostat/config', {
                'name':          f'{rko} 온도조절기',
                'mode_cmd_t':   f'kocom/room/thermo/{idx}/heat_mode/command',
                'mode_stat_t':   f'kocom/room/thermo/{idx}/state',
                'mode_stat_tpl': '{{ value_json.heat_mode }}',
                'temp_cmd_t':   f'kocom/room/thermo/{idx}/set_temp/command',
                'temp_stat_t':   f'kocom/room/thermo/{idx}/state',
                'temp_stat_tpl': '{{ value_json.set_temp }}',
                'curr_temp_t':   f'kocom/room/thermo/{idx}/state',
                'curr_temp_tpl': '{{ value_json.cur_temp }}',
                'modes': ['off', 'heat'],
                'min_temp': 18, 'max_temp': 30, 'temp_step': 1,
                'qos': 0,
                'uniq_id': f'kocom_wallpad_thermo_{room}',
                'device':  _BASE_DEVICE,
            })

        elif dev == 'gas':
            await pub(f'homeassistant/switch/kocom_{room}_gas/config', {
                'name':    f'{rko} 가스밸브',
                'cmd_t':   f'kocom/{room}/gas/command',
                'stat_t':  f'kocom/{room}/gas/state',
                'val_tpl': '{{ value_json.state }}',
                'pl_on': 'on', 'pl_off': 'off',
                'ic': 'mdi:gas-cylinder', 'qos': 0,
                'uniq_id': f'kocom_wallpad_gas_{room}',
                'device':  _BASE_DEVICE,
            })

        elif dev == 'elevator':
            await pub('homeassistant/switch/kocom_elevator/config', {
                'name':    '엘리베이터',
                'cmd_t':   'kocom/myhome/elevator/command',
                'stat_t':  'kocom/myhome/elevator/state',
                'val_tpl': '{{ value_json.state }}',
                'pl_on': 'on', 'pl_off': 'off',
                'ic': 'mdi:elevator', 'qos': 0,
                'uniq_id': 'kocom_wallpad_elevator',
                'device':  _BASE_DEVICE,
            })
            for sub, uid, icon, name_ko in [
                ('floor',     'elev_floor', 'mdi:floor-plan',    '엘리베이터 층수'),
                ('direction', 'elev_dir',   'mdi:arrow-up-down', '엘리베이터 방향'),
            ]:
                await pub(f'homeassistant/sensor/kocom_elevator_{sub}/config', {
                    'name':    name_ko,
                    'stat_t':  'kocom/myhome/elevator/state',
                    'val_tpl': '{{ value_json.' + sub + ' }}',
                    'ic': icon, 'qos': 0,
                    'uniq_id': f'kocom_wallpad_{uid}',
                    'device':  _BASE_DEVICE,
                })

        elif dev == 'aircon':
            await pub(f'homeassistant/climate/kocom_{room}_aircon/config', {
                'name':              f'{rko} 에어컨',
                'mode_cmd_t':       f'kocom/{room}/aircon/hvac/command',
                'mode_stat_t':       f'kocom/{room}/aircon/state',
                'mode_stat_tpl':     '{{ value_json.hvac_mode }}',
                'fan_mode_cmd_t':   f'kocom/{room}/aircon/fan/command',
                'fan_mode_stat_t':   f'kocom/{room}/aircon/state',
                'fan_mode_stat_tpl': '{{ value_json.fan_mode }}',
                'temp_cmd_t':       f'kocom/{room}/aircon/temp/command',
                'temp_stat_t':       f'kocom/{room}/aircon/state',
                'temp_stat_tpl':     '{{ value_json.set_temp }}',
                'curr_temp_t':       f'kocom/{room}/aircon/state',
                'curr_temp_tpl':     '{{ value_json.cur_temp }}',
                'modes':     ['off', 'cool', 'fan_only', 'dry', 'auto'],
                'fan_modes': ['low', 'medium', 'high', 'auto'],
                'min_temp': 18, 'max_temp': 30, 'temp_step': 1,
                'qos': 0,
                'uniq_id': f'kocom_wallpad_aircon_{room}',
                'device':  _BASE_DEVICE,
            })

        elif dev == 'motion':
            await pub(f'homeassistant/binary_sensor/kocom_{room}_motion/config', {
                'name':    f'{rko} 동작감지',
                'stat_t':  f'kocom/{room}/motion/state',
                'val_tpl': '{{ value_json.state }}',
                'pl_on': 'on', 'pl_off': 'off',
                'dev_cla': 'motion', 'qos': 0,
                'uniq_id': f'kocom_wallpad_motion_{room}',
                'device':  _BASE_DEVICE,
            })

        elif dev == 'airquality':
            for aq_key, label, dev_class, unit in [
                ('pm10',     'PM10',  'pm10',                       'µg/m³'),
                ('pm25',     'PM2.5', 'pm25',                       'µg/m³'),
                ('co2',      'CO₂',   'carbon_dioxide',             'ppm'),
                ('voc',      'VOC',   'volatile_organic_compounds', 'µg/m³'),
                ('temp',     '온도',  'temperature',                '°C'),
                ('humidity', '습도',  'humidity',                   '%'),
            ]:
                await pub(f'homeassistant/sensor/kocom_{room}_aq_{aq_key}/config', {
                    'name':         f'{rko} 공기질 {label}',
                    'stat_t':       f'kocom/{room}/airquality/state',
                    'val_tpl':      '{{ value_json.' + aq_key + ' }}',
                    'dev_cla':      dev_class,
                    'unit_of_meas': unit,
                    'qos': 0,
                    'uniq_id': f'kocom_wallpad_aq_{room}_{aq_key}',
                    'device':  _BASE_DEVICE,
                })

    # 수동 전체 조회 버튼
    await pub('homeassistant/button/kocom_wallpad_query/config', {
        'name':   '전체 상태 조회',
        'cmd_t':  'kocom/myhome/query/command',
        'pl_prs': 'PRESS', 'qos': 0,
        'uniq_id': 'kocom_wallpad_query',
        'device':  _BASE_DEVICE,
    })

    log.info('[Discovery] HA MQTT discovery published.')
