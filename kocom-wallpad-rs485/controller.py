"""Kocom RS485 packet parser and command builder."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, List

from const import (
    PACKET_PREFIX, PACKET_SUFFIX, PACKET_LEN,
    CODE_DEVICE, ROOM_NAME, ROOM_CODE,
    AIRCON_HVAC_CODE, AIRCON_HVAC_NAME,
    AIRCON_FAN_CODE, AIRCON_FAN_NAME,
    VENT_PRESET_NAME, VENT_PRESET_CODE,
    ELEVATOR_DIR,
)
from models import PacketFrame

log = logging.getLogger(__name__)

StateCallback = Callable[[str, dict], Awaitable[None]]


class KocomController:
    """
    RS485 패킷 파싱 및 커맨드 패킷 생성.

    수신 패킷을 파싱해 on_state 콜백으로 결과를 전달하고,
    build_command / build_query 로 송신 패킷 바이트를 생성한다.
    """

    def __init__(self, on_state: StateCallback, config) -> None:
        self._on_state      = on_state
        self._config        = config
        self._rx_buf        = bytearray()
        self._state_cache:   dict[str, dict]  = {}
        self._device_storage: dict[str, Any]  = {}

    # ── 수신 파이프라인 ──────────────────────────────────────────
    def feed(self, chunk: bytes) -> None:
        """수신 바이트를 버퍼에 추가하고 완성된 패킷마다 파싱 태스크를 생성."""
        self._rx_buf.extend(chunk)
        loop = asyncio.get_running_loop()
        for raw in self._extract_packets():
            log.debug('[RX] %s', raw.hex())
            loop.create_task(self._dispatch(PacketFrame(raw)))

    def _extract_packets(self) -> List[bytes]:
        packets: List[bytes] = []
        buf = self._rx_buf
        while True:
            start = buf.find(PACKET_PREFIX)
            if start < 0:
                buf.clear()
                break
            if start > 0:
                del buf[:start]
            if len(buf) < PACKET_LEN:
                break
            candidate = bytes(buf[:PACKET_LEN])
            if not candidate.endswith(PACKET_SUFFIX):
                del buf[0]
                continue
            packets.append(candidate)
            del buf[:PACKET_LEN]
        return packets

    # ── 디스패치 ────────────────────────────────────────────────
    async def _dispatch(self, frame: PacketFrame) -> None:
        if not frame.is_valid:
            log.debug('[Parser] Invalid packet: %s', frame.raw.hex())
            return
        dt = frame.dev_type
        if dt == 'light':
            if frame.dev_room == 0xFF:
                await self._pub_cutoff(frame)
            else:
                await self._pub_switch(frame, 'light')
        elif dt == 'outlet':
            await self._pub_switch(frame, 'outlet')
        elif dt == 'thermo':
            await self._pub_thermo(frame)
        elif dt == 'aircon':
            await self._pub_aircon(frame)
        elif dt == 'fan':
            await self._pub_fan(frame)
        elif dt == 'gas':
            await self._pub_gas(frame)
        elif dt == 'elevator':
            await self._pub_elevator(frame)
        elif dt == 'motion':
            await self._pub_motion(frame)
        elif dt == 'airquality':
            await self._pub_airquality(frame)
        else:
            log.debug('[Parser] Unknown device code=0x%02x raw=%s', frame.dev_code, frame.raw.hex())

    # ── 발행 헬퍼 ────────────────────────────────────────────────
    async def _notify(self, topic: str, payload: dict) -> None:
        self._state_cache[topic] = payload
        await self._on_state(topic, payload)

    def _room(self, room_byte: int) -> str:
        return ROOM_NAME.get(room_byte, f'room{room_byte}')

    # ── 장치별 파싱 및 발행 ──────────────────────────────────────
    async def _pub_cutoff(self, frame: PacketFrame) -> None:
        state = 'on' if frame.command == 0x65 else 'off'
        await self._notify('kocom/myhome/lightcutoff/state', {'state': state})

    async def _pub_switch(self, frame: PacketFrame, dev: str) -> None:
        if frame.command != 0x00:
            return
        room  = self._room(frame.dev_room)
        count = self._config.get_switch_count(dev, room)
        if count == 1:
            # 단일 장치: 번호 없이 key = dev (e.g. 'light')
            state = {dev: ('on' if frame.payload[0] == 0xFF else 'off')}
        else:
            # 복수 장치: 순번 포함 key = dev_N (e.g. 'light_1', 'light_2')
            state = {
                f'{dev}_{i+1}': ('on' if frame.payload[i] == 0xFF else 'off')
                for i in range(count)
            }
        await self._notify(f'kocom/{room}/{dev}/state', state)

    async def _pub_thermo(self, frame: PacketFrame) -> None:
        if frame.command != 0x00:
            return
        idx        = frame.dev_room
        heat_mode  = 'heat' if (frame.payload[0] >> 4) == 0x01 else 'off'
        away       = (frame.payload[1] & 0x0F) == 0x01
        set_temp   = float(frame.payload[2])
        cur_temp   = float(frame.payload[4])
        hot_temp   = frame.payload[3]
        heat_temp  = frame.payload[5]
        error_code = frame.payload[6]

        if set_temp % 1 == 0.5:
            self._device_storage[f'thermo_{idx}_step'] = 0.5

        await self._notify(f'kocom/room/thermo/{idx}/state', {
            'heat_mode': heat_mode,
            'away':      'true' if away else 'false',
            'set_temp':  set_temp,
            'cur_temp':  cur_temp,
            'temp_step': self._device_storage.get(f'thermo_{idx}_step', 1.0),
        })
        if hot_temp > 0:
            await self._notify(f'kocom/room/thermo/{idx}/hot_temp',  {'value': hot_temp})
        if heat_temp > 0:
            await self._notify(f'kocom/room/thermo/{idx}/heat_temp', {'value': heat_temp})
        if error_code != 0:
            await self._notify(f'kocom/room/thermo/{idx}/error',     {'code': error_code})

    async def _pub_aircon(self, frame: PacketFrame) -> None:
        if frame.command != 0x00:
            return
        room  = self._room(frame.dev_room)
        hvac  = AIRCON_HVAC_NAME.get(frame.payload[1], 'off') if frame.payload[0] == 0x10 else 'off'
        fan   = AIRCON_FAN_NAME.get(frame.payload[2], 'low')
        await self._notify(f'kocom/{room}/aircon/state', {
            'hvac_mode': hvac,
            'fan_mode':  fan,
            'cur_temp':  float(frame.payload[4]),
            'set_temp':  float(frame.payload[5]),
        })

    async def _pub_fan(self, frame: PacketFrame) -> None:
        if frame.command != 0x00:
            return
        room        = self._room(frame.dev_room)
        on          = (frame.payload[0] >> 4) == 0x01
        preset      = VENT_PRESET_NAME.get(frame.payload[1], 'unknown')
        speed_byte  = frame.payload[2]
        speed_code  = speed_byte & 0xF0   # 상위 4비트: 0x40/0x80/0xC0
        timer_hours = speed_byte & 0x0F   # 하위 4비트: 0-15시간
        co2         = (frame.payload[4] * 100) + frame.payload[5]
        err         = frame.payload[6]
        await self._notify(f'kocom/{room}/fan/state', {
            'state':  'on' if on else 'off',
            'preset': preset,
            'speed':  speed_code,
            'timer':  timer_hours,
        })
        if co2 > 0:
            await self._notify(f'kocom/{room}/fan/co2',   {'value': co2})
        if err != 0:
            await self._notify(f'kocom/{room}/fan/error', {'code': err})

    async def _pub_gas(self, frame: PacketFrame) -> None:
        if frame.command not in (0x01, 0x02):
            return
        room  = self._room(frame.dev_room)
        state = 'on' if frame.command == 0x01 else 'off'
        await self._notify(f'kocom/{room}/gas/state', {'state': state})

    async def _pub_elevator(self, frame: PacketFrame) -> None:
        active = frame.payload[0] in (0x01, 0x02) or frame.packet_type == 0x0D
        if frame.payload[0] == 0x03:
            active = False

        direction = (
            'called' if frame.payload[0] == 0x00 and frame.packet_type == 0x0D
            else ELEVATOR_DIR.get(frame.payload[0], 'unknown')
        )

        floor = 'unknown'
        b1, b2 = frame.payload[1], frame.payload[2]
        if b1 != 0x00:
            if b2 != 0x00:
                floor = f'{chr(b1)}{chr(b2)}'
            elif b1 >> 4 == 0x08:
                floor = f'B{b1 & 0x0F}'
            else:
                floor = str(b1)

        state: dict = {'state': 'on' if active else 'off', 'direction': direction, 'floor': floor}

        rs485_floor = int(self._config.get('Elevator', 'rs485_floor', fallback='0'))
        if rs485_floor != 0 and floor not in ('unknown', ''):
            try:
                if int(floor) == rs485_floor:
                    state['state'] = 'off'
                    state['direction'] = 'arrival'
            except ValueError:
                pass

        await self._notify('kocom/myhome/elevator/state', state)

    async def _pub_motion(self, frame: PacketFrame) -> None:
        if frame.command not in (0x00, 0x04):
            return
        room  = self._room(frame.dev_room)
        state = 'on' if frame.command == 0x04 else 'off'
        await self._notify(f'kocom/{room}/motion/state', {'state': state})

    async def _pub_airquality(self, frame: PacketFrame) -> None:
        if frame.command not in (0x00, 0x3A):
            return
        room = self._room(frame.dev_room)
        co2  = int.from_bytes(frame.payload[2:4], 'big')
        voc  = int.from_bytes(frame.payload[4:6], 'big')
        await self._notify(f'kocom/{room}/airquality/state', {
            'pm10':     frame.payload[0],
            'pm25':     frame.payload[1],
            'co2':      co2,
            'voc':      voc,
            'temp':     frame.payload[6],
            'humidity': frame.payload[7],
        })

    # ── 패킷 생성 ────────────────────────────────────────────────
    @staticmethod
    def _make_packet(
        dest_dev: int, dest_room: int,
        src_dev:  int, src_room:  int,
        command:  int, data: bytes,
    ) -> bytes:
        body = (
            bytes([0x30, 0xBC, 0x00])
            + bytes([dest_dev, dest_room])
            + bytes([src_dev,  src_room])
            + bytes([command])
            + data
        )
        chk = sum(body) % 256
        return PACKET_PREFIX + body + bytes([chk]) + PACKET_SUFFIX

    def build_query(self, dev: str, room: str) -> bytes:
        """장치 상태 조회 패킷 생성."""
        dev_code  = CODE_DEVICE[dev]
        room_code = ROOM_CODE.get(room, 0x00)
        return self._make_packet(dev_code, room_code, 0x01, 0x00, 0x3A, bytes(8))

    def build_command(self, dev: str, room: str, action: str, **kwargs) -> List[bytes]:
        """
        MQTT 커맨드 → RS485 패킷 목록 변환.

        Returns a list because some operations (multi-light) emit multiple packets.
        """
        if dev in ('light', 'outlet'):
            return self._build_switch(dev, room, action, **kwargs)
        if dev == 'thermo':
            return [self._build_thermo(room, action, **kwargs)]
        if dev == 'aircon':
            return [self._build_aircon(room, action, **kwargs)]
        if dev == 'fan':
            return [self._build_fan(room, action, **kwargs)]
        if dev == 'gas':
            return [self._build_gas(room)]
        if dev == 'elevator':
            return [self._build_elevator()]
        raise ValueError(f'build_command: unsupported device "{dev}"')

    # ── 장치별 커맨드 빌더 ───────────────────────────────────────
    def _build_switch(self, dev: str, room: str, action: str, **kwargs) -> List[bytes]:
        """
        조명/콘센트 on/off 패킷.

        kwargs:
          index (int): 장치 번호 (1-based). 두 자리 숫자(e.g. 12)는 1번·2번 동시 제어.
        """
        dest_dev  = CODE_DEVICE[dev]
        dest_room = ROOM_CODE.get(room, 0x00)
        cache_key = f'kocom/{room}/{dev}/state'
        cached    = self._state_cache.get(cache_key, {})

        count = self._config.get_switch_count(dev, room)
        data  = bytearray(8)
        if count == 1:
            if cached.get(dev) == 'on':
                data[0] = 0xFF
        else:
            for i in range(8):
                if cached.get(f'{dev}_{i+1}') == 'on':
                    data[i] = 0xFF

        onoff   = 0xFF if action == 'on' else 0x00
        packets = []
        n = kwargs.get('index', 1)
        while n > 0:
            idx = n % 10
            if idx > 0:
                data[idx - 1] = onoff
                packets.append(self._make_packet(dest_dev, dest_room, 0x01, 0x00, 0x00, bytes(data)))
            n //= 10
        return packets

    def _build_thermo(self, room: str, action: str, **kwargs) -> bytes:
        """
        온도조절기 커맨드 패킷.

        action: 'heat_mode' | 'set_temp'
        kwargs: heat_mode='heat'|'off' | set_temp=23
        """
        try:
            room_code = int(room)   # kocom/room/thermo/{idx}/... 토픽의 숫자 인덱스
        except ValueError:
            room_code = ROOM_CODE.get(room, 0x00)
        data = bytearray(8)
        if action == 'heat_mode':
            data[0] = 0x11 if kwargs.get('heat_mode') == 'heat' else 0x00
            data[2] = int(self._config.get('User', 'init_temp', fallback='23'))
        elif action == 'set_temp':
            data[0] = 0x11
            data[2] = int(float(kwargs['set_temp']))
        return self._make_packet(CODE_DEVICE['thermo'], room_code, 0x01, 0x00, 0x00, bytes(data))

    def _build_aircon(self, room: str, action: str, **kwargs) -> bytes:
        """
        에어컨 커맨드 패킷.

        action: 'hvac' | 'fan' | 'temp'
        """
        dest_dev, dest_room = CODE_DEVICE['aircon'], ROOM_CODE.get(room, 0x00)
        data = bytearray(8)
        if action == 'hvac':
            cmd = kwargs.get('hvac', 'off')
            if cmd == 'off':
                data[0] = 0x00
            else:
                data[0] = 0x10
                data[1] = AIRCON_HVAC_CODE.get(cmd, 0x00)
        elif action == 'fan':
            data[0] = 0x10
            data[2] = AIRCON_FAN_CODE.get(kwargs.get('fan', 'low'), 0x01)
        elif action == 'temp':
            data[0] = 0x10
            data[5] = int(float(kwargs['temp']))
        return self._make_packet(dest_dev, dest_room, 0x01, 0x00, 0x00, bytes(data))

    def _build_fan(self, room: str, action: str, **kwargs) -> bytes:
        """
        환기장치 커맨드 패킷.

        action: 'on' | 'off' | 'preset' | 'speed'
        kwargs: preset='auto' | speed=<percentage 1-100>
        """
        dest_dev, dest_room = CODE_DEVICE['fan'], ROOM_CODE.get(room, 0x00)
        data      = bytearray(8)
        fan_state = self._state_cache.get(f'kocom/{room}/fan/state', {})
        cur_speed = fan_state.get('speed', 0x80)   # 캐시된 현재 속도 (기본 Medium)
        cur_timer = fan_state.get('timer', 0)       # 캐시된 현재 타이머

        if action == 'preset':
            preset = kwargs.get('preset', 'ventilation')
            if preset in ('Off', 'off'):
                data[0] = 0x00
                data[2] = (cur_speed & 0xF0) | (cur_timer & 0x0F)
            else:
                # sleep 모드: 속도 1단계(약풍) + 꺼짐 예약 8시간 자동 적용
                speed   = 0x40 if preset == 'sleep' else cur_speed
                timer   = 8    if preset == 'sleep' else cur_timer
                data[0] = 0x11
                data[1] = VENT_PRESET_CODE.get(preset, 0x01)
                data[2] = (speed & 0xF0) | (timer & 0x0F)

        elif action == 'speed':
            # HA가 spd_rng_min=1, spd_rng_max=3 범위로 0(꺼짐)/1/2/3을 전송
            level = int(float(kwargs.get('speed', 2)))
            if level == 0:
                data[0] = 0x00
                data[2] = (cur_speed & 0xF0) | (cur_timer & 0x0F)
            else:
                speed_code = {1: 0x40, 2: 0x80, 3: 0xC0}.get(level, 0x80)
                data[0] = 0x11
                data[2] = speed_code | (cur_timer & 0x0F)

        elif action == 'timer':
            hours   = max(0, min(12, int(float(kwargs.get('hours', 0)))))
            data[0] = 0x11
            data[2] = (cur_speed & 0xF0) | hours

        else:  # on / off
            _speed = {'Low': 0x40, 'Medium': 0x80, 'High': 0xC0}
            init   = self._config.get('User', 'init_fan_mode', fallback='Medium')
            if action == 'on':
                data[0] = 0x11
                data[1] = VENT_PRESET_CODE.get('auto', 0x02)
                data[2] = _speed.get(init, 0x80)
            else:  # off
                data[0] = 0x00
                data[2] = (cur_speed & 0xF0) | (cur_timer & 0x0F)

        return self._make_packet(dest_dev, dest_room, 0x01, 0x00, 0x00, bytes(data))

    def _build_gas(self, room: str) -> bytes:
        """가스밸브 차단 패킷 (off 전용)."""
        dest_dev, dest_room = CODE_DEVICE['gas'], ROOM_CODE.get(room, 0x00)
        return self._make_packet(dest_dev, dest_room, 0x01, 0x00, 0x02, bytes(8))

    def _build_elevator(self) -> bytes:
        """RS485 엘리베이터 호출 패킷."""
        return self._make_packet(0x01, 0x00, CODE_DEVICE['elevator'], 0x00, 0x01, bytes(8))
