"""Home Assistant MQTT Discovery 페이로드 발행 모듈.

Home Assistant(HA)는 `homeassistant/<컴포넌트>/<노드ID>/config` 형식의
MQTT 토픽으로 발행된 JSON 설정을 수신하면, 별도의 YAML 설정 없이도
해당 엔티티(조명, 스위치, 센서 등)를 자동으로 등록한다.
이 모듈은 애드온 설정(config.json)의 devices 목록을 읽어
각 기기에 대응하는 Discovery 설정을 MQTT 브로커에 발행하는 역할을 한다.

발행되는 페이로드에는 HA 공식 축약 키(abbreviation)를 사용한다:
    name          : HA에 표시될 엔티티 이름
    cmd_t         : command_topic   — HA → 애드온으로 제어 명령을 보내는 토픽
    stat_t        : state_topic    — 애드온 → HA로 상태를 보고하는 토픽
    val_tpl       : value_template — 상태 JSON에서 값을 추출하는 Jinja2 템플릿
    stat_val_tpl  : state_value_template (light 컴포넌트용 상태 템플릿)
    pl_on/pl_off  : payload_on / payload_off — 켜짐/꺼짐을 나타내는 값
    pl_prs        : payload_press — 버튼이 눌렸을 때 발행되는 값
    dev_cla       : device_class  — HA 기기 분류(아이콘·단위 자동 지정)
    unit_of_meas  : unit_of_measurement — 측정 단위
    ic            : icon — MDI 아이콘 이름
    uniq_id       : unique_id — HA 내부에서 엔티티를 식별하는 고유 ID
    device        : 엔티티가 소속될 "기기" 정보(같은 device면 HA에서 한 기기로 묶임)
"""

from __future__ import annotations

import json
import logging
from collections import Counter

import aiomqtt  # type: ignore

from const import SW_VERSION, ROOM_CODE

log = logging.getLogger(__name__)

# 세대 공용 기능(엘리베이터, 전체 조회 버튼 등)이 소속되는 "월패드 본체" 기기.
# 방별 기기들은 via_device로 이 기기를 허브로 참조한다.
# 같은 'ids'를 가진 엔티티들은 HA에서 하나의 기기로 묶인다.
#   name : HA 기기 목록에 표시될 이름
#   ids  : 기기 고유 식별자(identifiers)
#   mf   : 제조사(manufacturer)
#   mdl  : 모델명(model)
#   sw   : 소프트웨어 버전(sw_version)
_BASE_DEVICE = {
    'name': '코콤 스마트 월패드',
    'ids':  'kocom_smart_wallpad',
    'mf':   'KOCOM',
    'mdl':  '스마트 월패드',
    'sw':   SW_VERSION,
}

# 설정 파일에서 사용하는 영문 방 이름 → HA에 표시할 한국어 이름 매핑.
# 여기에 없는 방 이름은 영문 그대로 표시된다(_rname 참고).
_ROOM_KO: dict[str, str] = {
    'livingroom': '거실',
    'room1':      '방1',
    'room2':      '방2',
    'room3':      '방3',
    'kitchen':    '주방',
}


def _rname(room: str) -> str:
    """영문 방 이름을 한국어 표시 이름으로 변환한다. 매핑에 없으면 원문 반환."""
    return _ROOM_KO.get(room, room)


def _sub_device(room: str, kind: str, kind_ko: str) -> dict:
    """방 안의 물리 모듈 단위 기기(device) 정보를 생성한다.

    HA의 device는 "물리적으로 하나의 하드웨어 모듈"을 표현해야 하므로,
    방 전체를 하나의 device로 묶지 않고 (방, 기기종류) 조합마다
    별도의 device를 만든다. 예: 거실 조명 스위치 모듈, 거실 온도조절기.
    같은 모듈이 제어하는 여러 엔티티(조명 1·2 등)는 같은 device에 묶인다.

    방 단위 그룹핑은 device가 아니라 suggested_area(HA 구역)로 처리하고,
    via_device로 월패드 본체(_BASE_DEVICE)를 허브로 연결해
    HA 기기 목록에서 계층 구조가 보이도록 한다.

    Args:
        room:    영문 방 이름 (예: 'livingroom')
        kind:    기기 종류의 영문 슬러그 — ids에 사용 (예: 'light')
        kind_ko: 기기 종류의 한국어 이름 — 표시 이름에 사용 (예: '조명')
    """
    rko = _rname(room)
    return {
        'name': f'코콤 {rko} {kind_ko}',
        'ids':  f'kocom_wallpad_{room}_{kind}',
        'mf':   'KOCOM',
        'mdl':  '스마트 월패드',
        'sw':   SW_VERSION,
        'suggested_area': rko,                    # HA 구역(Area) 자동 지정 제안
        'via_device': 'kocom_smart_wallpad',      # 허브(월패드 본체)와 연결
    }


async def publish_discovery(mqtt: aiomqtt.Client, config) -> None:
    """devices 목록을 순회하며 HA MQTT Discovery 설정 발행.

    처리 흐름:
      1. 설정에서 기기 목록을 읽는다.
      2. 같은 (기기종류, 방) 조합이 몇 번 등장하는지 센다.
         - 1개뿐이면 순번 없이 "거실 조명"처럼 단일 이름 사용
         - 2개 이상이면 "거실 조명 1", "거실 조명 2"처럼 순번을 붙임
      3. 기기 종류(type)별로 알맞은 HA 컴포넌트의 Discovery 페이로드를 발행한다.
      4. 마지막에 전체 상태를 수동 조회하는 버튼 엔티티를 발행한다.

    모든 발행은 retain=True로 이루어지므로, HA가 재시작하더라도
    브로커에 남아 있는 설정을 즉시 다시 받아 엔티티를 복원할 수 있다.
    """
    dev_list = config.get_devices()

    # (type, room) 조합의 총 등장 횟수 — 단일/복수 판별에 사용
    occ_total: Counter = Counter(
        (d.get('type'), d.get('room', 'livingroom')) for d in dev_list
    )
    # 순회 중 각 (type, room)의 현재 순번 추적 (1부터 증가)
    occ_seq: dict[tuple, int] = {}

    async def pub(topic: str, payload: dict) -> None:
        """dict 페이로드를 JSON으로 직렬화해 retain 플래그와 함께 발행한다."""
        await mqtt.publish(topic, json.dumps(payload), retain=True)

    for entry in dev_list:
        dev  = entry.get('type', '')                 # 기기 종류 (light, outlet, fan, ...)
        room = entry.get('room', 'livingroom')       # 방 이름 (미지정 시 거실)
        rko  = _rname(room)                          # 한국어 방 이름
        key  = (dev, room)

        # 순번 계산: 복수면 1, 2, … / 단일이면 None
        total = occ_total[key]
        if total > 1:
            occ_seq[key] = occ_seq.get(key, 0) + 1
            n: int | None = occ_seq[key]
        else:
            n = None

        # ── 조명 (light 컴포넌트) ─────────────────────────────────────────
        # 단일 조명과 복수 조명은 토픽·이름 규칙이 다르다.
        #   단일: kocom/<room>/light/command,   상태 키 value_json.light
        #   복수: kocom/<room>/light/<n>/command, 상태 키 value_json.light_<n>
        # 상태 토픽은 방 전체가 하나를 공유하고, 템플릿으로 각자 값을 뽑는다.
        if dev == 'light':
            if n is None:
                await pub(f'homeassistant/light/kocom_{room}_light/config', {
                    'name':         f'{rko} 조명',
                    'cmd_t':        f'kocom/{room}/light/command',
                    'stat_t':       f'kocom/{room}/light/state',
                    'stat_val_tpl': '{{ value_json.light }}',
                    'pl_on': 'on', 'pl_off': 'off', 'qos': 0,
                    'uniq_id': f'kocom_wallpad_light_{room}',
                    'device':  _sub_device(room, 'light', '조명'),
                })
            else:
                await pub(f'homeassistant/light/kocom_{room}_light{n}/config', {
                    'name':         f'{rko} 조명 {n}',
                    'cmd_t':        f'kocom/{room}/light/{n}/command',
                    'stat_t':       f'kocom/{room}/light/state',
                    # 공유 상태 JSON에서 light_1, light_2 … 중 자기 값만 추출
                    'stat_val_tpl': '{{ value_json.light_' + str(n) + ' }}',
                    'pl_on': 'on', 'pl_off': 'off', 'qos': 0,
                    # 같은 스위치 모듈이 제어하므로 조명 1·2는 한 device에 묶인다
                    'uniq_id': f'kocom_wallpad_light_{room}_{n}',
                    'device':  _sub_device(room, 'light', '조명'),
                })

        # ── 콘센트 (switch 컴포넌트, device_class=outlet) ────────────────
        # 조명과 동일한 단일/복수 규칙을 따르며, HA에서는 스위치로 등록되지만
        # dev_cla='outlet' 지정으로 콘센트 아이콘·분류가 적용된다.
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
                    'device':  _sub_device(room, 'outlet', '콘센트'),
                })
            else:
                await pub(f'homeassistant/switch/kocom_{room}_outlet{n}/config', {
                    'name':    f'{rko} 콘센트 {n}',
                    'cmd_t':   f'kocom/{room}/outlet/{n}/command',
                    'stat_t':  f'kocom/{room}/outlet/state',
                    # 공유 상태 JSON에서 outlet_1, outlet_2 … 중 자기 값만 추출
                    'val_tpl': '{{ value_json.outlet_' + str(n) + ' }}',
                    'pl_on': 'on', 'pl_off': 'off',
                    'dev_cla': 'outlet', 'qos': 0,
                    # 같은 모듈이 제어하므로 콘센트 1·2는 한 device에 묶인다
                    'uniq_id': f'kocom_wallpad_outlet_{room}_{n}',
                    'device':  _sub_device(room, 'outlet', '콘센트'),
                })

        # ── 환기장치 (fan 컴포넌트) + 부속 센서 2종 ──────────────────────
        # 환기장치 본체 1개와, 같은 기기에 묶이는 CO₂ 센서·예약 끄기 타이머
        # 센서까지 총 3개의 엔티티를 발행한다.
        elif dev == 'fan':
            await pub(f'homeassistant/fan/kocom_{room}_fan/config', {
                'name':            f'{rko} 환기장치',
                'cmd_t':           f'kocom/{room}/fan/command',
                'stat_t':          f'kocom/{room}/fan/state',
                'stat_val_tpl':    '{{ value_json.state }}',
                # 프리셋 모드(환기/자동/바이패스/취침/공기청정) 상태·명령 토픽
                'pr_mode_stat_t':  f'kocom/{room}/fan/state',
                'pr_mode_val_tpl': '{{ value_json.preset }}',
                'pr_mode_cmd_t':   f'kocom/{room}/fan/set_preset_mode/command',
                'pr_mode_cmd_tpl': '{{ value }}',
                'pr_modes': ['ventilation', 'auto', 'bypass', 'sleep', 'air purification'],
                # 풍량: 월패드 원시값(64/128/192)을 HA 단계값(1/2/3)으로 변환
                'pct_cmd_t':       f'kocom/{room}/fan/set_speed/command',
                'pct_stat_t':      f'kocom/{room}/fan/state',
                'pct_val_tpl':     '{{ {64: 1, 128: 2, 192: 3}.get(value_json.speed | int, 0) }}',
                'spd_rng_min': 1,   # 풍량 최소 단계
                'spd_rng_max': 3,   # 풍량 최대 단계
                'pl_on': 'on', 'pl_off': 'off', 'qos': 0,
                'uniq_id': f'kocom_wallpad_fan_{room}',
                'device':  _sub_device(room, 'fan', '환기장치'),
            })
            # 환기장치에 내장된 CO₂ 농도 센서 (단위: ppm)
            await pub(f'homeassistant/sensor/kocom_{room}_fan_co2/config', {
                'name':         f'{rko} CO₂',
                'stat_t':       f'kocom/{room}/fan/co2',
                'val_tpl':      '{{ value_json.value }}',
                'dev_cla':      'carbon_dioxide',
                'unit_of_meas': 'ppm',
                'ic':           'mdi:molecule-co2',
                'qos': 0,
                # CO₂ 센서는 환기장치에 내장되어 있으므로 같은 device에 묶는다
                'uniq_id': f'kocom_wallpad_fan_co2_{room}',
                'device':  _sub_device(room, 'fan', '환기장치'),
            })
            # 환기장치 예약 끄기까지 남은 시간 센서 (단위: 시간)
            await pub(f'homeassistant/sensor/kocom_{room}_fan_timer/config', {
                'name':         f'{rko} 환기장치 예약 끄기',
                'stat_t':       f'kocom/{room}/fan/state',
                'val_tpl':      '{{ value_json.timer }}',
                'unit_of_meas': 'h',
                'ic':           'mdi:timer-outline',
                'qos': 0,
                # 타이머 역시 환기장치 본체 기능이므로 같은 device에 묶는다
                'uniq_id': f'kocom_wallpad_fan_timer_{room}',
                'device':  _sub_device(room, 'fan', '환기장치'),
            })

        # ── 난방 온도조절기 (climate 컴포넌트) ───────────────────────────
        # 다른 기기와 달리 토픽 경로에 방 이름 대신 월패드 프로토콜상의
        # 방 번호(ROOM_CODE, 예: livingroom=0)를 사용한다.
        elif dev == 'thermo':
            idx = ROOM_CODE.get(room, 0)  # 프로토콜상 방 인덱스 (미등록 방은 0)
            await pub(f'homeassistant/climate/kocom_{room}_thermostat/config', {
                'name':          f'{rko} 온도조절기',
                # 운전 모드(off/heat) 명령·상태
                'mode_cmd_t':   f'kocom/room/thermo/{idx}/heat_mode/command',
                'mode_stat_t':   f'kocom/room/thermo/{idx}/state',
                'mode_stat_tpl': '{{ value_json.heat_mode }}',
                # 희망(설정) 온도 명령·상태
                'temp_cmd_t':   f'kocom/room/thermo/{idx}/set_temp/command',
                'temp_stat_t':   f'kocom/room/thermo/{idx}/state',
                'temp_stat_tpl': '{{ value_json.set_temp }}',
                # 현재 실내 온도
                'curr_temp_t':   f'kocom/room/thermo/{idx}/state',
                'curr_temp_tpl': '{{ value_json.cur_temp }}',
                'modes': ['off', 'heat'],
                'min_temp': 18, 'max_temp': 30, 'temp_step': 1,  # 설정 가능 범위 18~30°C, 1°C 단위
                'qos': 0,
                'uniq_id': f'kocom_wallpad_thermo_{room}',
                'device':  _sub_device(room, 'thermo', '온도조절기'),
            })

        # ── 가스밸브 (switch 컴포넌트) ───────────────────────────────────
        # 월패드 특성상 잠금(off)만 가능하고 열기는 불가한 경우가 많지만,
        # HA에는 일반 스위치로 노출한다.
        elif dev == 'gas':
            await pub(f'homeassistant/switch/kocom_{room}_gas/config', {
                'name':    f'{rko} 가스밸브',
                'cmd_t':   f'kocom/{room}/gas/command',
                'stat_t':  f'kocom/{room}/gas/state',
                'val_tpl': '{{ value_json.state }}',
                'pl_on': 'on', 'pl_off': 'off',
                'ic': 'mdi:gas-cylinder', 'qos': 0,
                'uniq_id': f'kocom_wallpad_gas_{room}',
                'device':  _sub_device(room, 'gas', '가스밸브'),
            })

        # ── 엘리베이터 호출 (switch) + 층수/방향 센서 ────────────────────
        # 세대 공용 기능이므로 방 구분 없이 고정 토픽(kocom/myhome/…)을 쓰고,
        # 기기 정보도 방별이 아닌 공용 _BASE_DEVICE에 묶는다.
        elif dev == 'elevator':
            # 호출 스위치: 켜면 엘리베이터를 호출하고, 도착하면 상태가 꺼짐으로 복귀
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
            # 현재 층수·이동 방향을 표시하는 보조 센서 2개
            # (sub: 상태 JSON 키, uid: unique_id 접미사, icon: 아이콘, name_ko: 표시 이름)
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

        # ── 에어컨 (climate 컴포넌트) ────────────────────────────────────
        # 운전 모드(냉방/송풍/제습/자동), 풍량, 희망 온도, 현재 온도를 지원.
        elif dev == 'aircon':
            await pub(f'homeassistant/climate/kocom_{room}_aircon/config', {
                'name':              f'{rko} 에어컨',
                # 운전 모드 명령·상태
                'mode_cmd_t':       f'kocom/{room}/aircon/hvac/command',
                'mode_stat_t':       f'kocom/{room}/aircon/state',
                'mode_stat_tpl':     '{{ value_json.hvac_mode }}',
                # 풍량(팬 모드) 명령·상태
                'fan_mode_cmd_t':   f'kocom/{room}/aircon/fan/command',
                'fan_mode_stat_t':   f'kocom/{room}/aircon/state',
                'fan_mode_stat_tpl': '{{ value_json.fan_mode }}',
                # 희망(설정) 온도 명령·상태
                'temp_cmd_t':       f'kocom/{room}/aircon/temp/command',
                'temp_stat_t':       f'kocom/{room}/aircon/state',
                'temp_stat_tpl':     '{{ value_json.set_temp }}',
                # 현재 실내 온도
                'curr_temp_t':       f'kocom/{room}/aircon/state',
                'curr_temp_tpl':     '{{ value_json.cur_temp }}',
                'modes':     ['off', 'cool', 'fan_only', 'dry', 'auto'],
                'fan_modes': ['low', 'medium', 'high', 'auto'],
                'min_temp': 18, 'max_temp': 30, 'temp_step': 1,  # 설정 가능 범위 18~30°C
                'qos': 0,
                'uniq_id': f'kocom_wallpad_aircon_{room}',
                'device':  _sub_device(room, 'aircon', '에어컨'),
            })

        # ── 동작 감지 센서 (binary_sensor 컴포넌트, device_class=motion) ─
        elif dev == 'motion':
            await pub(f'homeassistant/binary_sensor/kocom_{room}_motion/config', {
                'name':    f'{rko} 동작감지',
                'stat_t':  f'kocom/{room}/motion/state',
                'val_tpl': '{{ value_json.state }}',
                'pl_on': 'on', 'pl_off': 'off',
                'dev_cla': 'motion', 'qos': 0,
                'uniq_id': f'kocom_wallpad_motion_{room}',
                'device':  _sub_device(room, 'motion', '동작감지'),
            })

        # ── 공기질 측정기 (sensor 컴포넌트 6종) ──────────────────────────
        # 하나의 상태 토픽을 공유하며, 항목별로 센서 엔티티를 나눠 발행한다.
        # (aq_key: 상태 JSON 키, label: 표시 이름, dev_class: HA 분류, unit: 단위)
        elif dev == 'airquality':
            for aq_key, label, dev_class, unit in [
                ('pm10',     'PM10',  'pm10',                       'µg/m³'),  # 미세먼지
                ('pm25',     'PM2.5', 'pm25',                       'µg/m³'),  # 초미세먼지
                ('co2',      'CO₂',   'carbon_dioxide',             'ppm'),    # 이산화탄소
                ('voc',      'VOC',   'volatile_organic_compounds', 'µg/m³'),  # 휘발성 유기화합물
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
                    # 측정 항목 6종은 하나의 공기질 측정기가 제공하므로 한 device에 묶는다
                    'uniq_id': f'kocom_wallpad_aq_{room}_{aq_key}',
                    'device':  _sub_device(room, 'airquality', '공기질 측정기'),
                })

    # ── 수동 전체 조회 버튼 (button 컴포넌트) ────────────────────────────
    # 설정된 기기와 무관하게 항상 발행. HA에서 이 버튼을 누르면
    # 애드온이 월패드에 등록된 모든 기기의 상태를 즉시 다시 조회한다.
    await pub('homeassistant/button/kocom_wallpad_query/config', {
        'name':   '전체 상태 조회',
        'cmd_t':  'kocom/myhome/query/command',
        'pl_prs': 'PRESS', 'qos': 0,
        'uniq_id': 'kocom_wallpad_query',
        'device':  _BASE_DEVICE,
    })

    log.info('[Discovery] HA MQTT discovery published.')
