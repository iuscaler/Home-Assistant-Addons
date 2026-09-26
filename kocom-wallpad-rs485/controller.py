"""Kocom RS485 packet parser and command builder."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
import time
from datetime import datetime
from typing import Any, Awaitable, Callable, List

from const import (
    PACKET_PREFIX, PACKET_SUFFIX, PACKET_LEN,
    PT_BROADCAST, PT_SEND, SEQ_FIRST,
    CMD_STATE, CMD_ON, CMD_OFF, CMD_MOTION, CMD_QUERY,
    CMD_TIME_SET, CMD_TIME_REQ, CMD_CUTOFF_ON, CMD_CUTOFF_OFF,
    CODE_DEVICE, ROOM_NAME, ROOM_CODE,
    AIRCON_HVAC_CODE, AIRCON_HVAC_NAME,
    AIRCON_FAN_CODE, AIRCON_FAN_NAME,
    VENT_PRESET_NAME, VENT_PRESET_CODE,
    ELEVATOR_EVENT,
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
        self._state_cache:   dict[str, dict]  = {}   # 커맨드 조립용 최신 상태
        self._pub_cache:     dict[str, dict]  = {}   # 발행 중복 억제용
        self._device_storage: dict[str, Any]  = {}

        # ── 진단 카운터 ─────────────────────────────────────────
        # 월패드·EW11이 살아 있는지 HA에서 확인하기 위한 지표.
        self.diag: dict[str, Any] = {
            'rx_total':       0,      # 프레임 경계가 잡힌 패킷 수
            'rx_invalid':     0,      # 체크섬·프레임 오류
            'rx_retry':       0,      # 시퀀스 니블이 재시도인 패킷 수
            'tx_total':       0,      # 송신한 패킷 수
            'last_rx':        None,   # 마지막 수신 시각 (time.time())
            'last_wallpad_time': None,  # 월패드가 알려준 시각 (datetime)
            'last_wallpad_time_rx': None,  # 그 시각을 받은 시점 (time.time())
        }

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
        self.diag['rx_total'] += 1
        self.diag['last_rx'] = time.time()

        if not frame.is_valid:
            self.diag['rx_invalid'] += 1
            log.debug('[Parser] Invalid packet: %s', frame.raw.hex())
            return
        if frame.retry_count:
            self.diag['rx_retry'] += 1

        # 시각 프레임은 장치 종류와 무관하게 같은 형식이므로 먼저 처리한다.
        if frame.command in (CMD_TIME_SET, CMD_TIME_REQ):
            await self._pub_time(frame, offset=1, with_seconds=False)
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
        elif dt == 'timesync':
            # 월패드가 0x86으로 보내는 시각 브로드캐스트 (YY MM DD HH MM SS)
            if frame.command == CMD_ON:
                await self._pub_time(frame, offset=0, with_seconds=True)
        elif dt == 'shutoff2':
            # 외출모드 진입 시에만 오는 무응답 명령. 정체 미상이라 발행하지 않는다.
            log.debug('[Parser] shutoff2(0x2D) cmd=0x%02x', frame.command)
        else:
            log.debug('[Parser] Unknown device code=0x%02x raw=%s', frame.dev_code, frame.raw.hex())

    # ── 발행 헬퍼 ────────────────────────────────────────────────
    async def _notify(self, topic: str, payload: dict) -> None:
        """상태를 캐시에 저장하고 MQTT로 발행한다.

        값이 직전과 같으면 발행을 생략한다. 월패드는 응답 없는 프레임을 3회까지
        재전송하므로(전체 소등, 엘리베이터 통보, 시각 브로드캐스트) 그대로 두면
        같은 값이 반복 발행된다. MQTT는 retain을 쓰므로 생략해도 HA가 값을
        잃지 않는다.
        """
        self._state_cache[topic] = payload
        if self._pub_cache.get(topic) == payload:
            return
        self._pub_cache[topic] = payload
        await self._on_state(topic, payload)

    async def set_state(self, topic: str, payload: dict) -> None:
        """애드온이 스스로 판단한 상태를 발행한다.

        응답 프레임이 없는 브로드캐스트(전체 소등)처럼 버스에서 확인할 방법이
        없는 경우에만 사용한다. _notify를 거치므로 캐시와 중복 억제가 유지된다.
        """
        await self._notify(topic, payload)

    def invalidate_pub_cache(self) -> None:
        """발행 중복 억제 캐시를 비운다.

        브로커가 재시작해 retain 메시지를 잃었을 수 있으므로, MQTT 재연결
        직후에 호출해 다음 상태 프레임부터 다시 발행되게 한다.
        """
        self._pub_cache.clear()

    def _room(self, room_byte: int) -> str:
        return ROOM_NAME.get(room_byte, f'room{room_byte}')

    # ── 장치별 파싱 및 발행 ──────────────────────────────────────
    async def _pub_cutoff(self, frame: PacketFrame) -> None:
        """전체 소등 브로드캐스트 (0x09, 방 코드 0xFF).

        0x65 = 소등, 0x66 = 해제. 조회(0x3A)는 상태를 담지 않으므로 무시한다.
        해제하면 장치가 소등 직전 상태를 스스로 복원하므로 개별 조명 상태는
        뒤따르는 방별 보고로 갱신된다.
        """
        if frame.command == CMD_CUTOFF_ON:
            state = 'on'
        elif frame.command == CMD_CUTOFF_OFF:
            state = 'off'
        else:
            return
        await self._notify('kocom/myhome/lightcutoff/state', {'state': state})

    async def _pub_switch(self, frame: PacketFrame, dev: str) -> None:
        # 장치가 스스로 보낸 0x0B 보고만 상태로 신뢰한다. 월패드 명령이나 ACK
        # 에코를 반영하면 장치가 거부한 채널까지 켜진 것으로 잘못 표시된다.
        if frame.command != CMD_STATE or not frame.is_state_report:
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
        if frame.command != CMD_STATE or not frame.is_state_report:
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
        if frame.command != CMD_STATE or not frame.is_state_report:
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
        if frame.command != CMD_STATE or not frame.is_state_report:
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
        """가스밸브/인덕션 상태.

        상태는 페이로드가 아니라 커맨드 바이트로 표현된다 (0x01 열림 / 0x02 차단).
        월패드가 보낸 명령 프레임과 그 ACK는 '요청'일 뿐이며, 실제 차단 완료는
        약 2.9초 뒤 장치가 보내는 0x0B 보고로 확인된다. 따라서 장치 발신
        보고만 신뢰한다 — 그래야 HA 상태가 실제 밸브 동작을 뒤따른다.

        이 주소는 가스밸브와 인덕션이 공유하므로, 인덕션을 켜도 '열림'으로
        보고된다. 프레임만으로는 둘을 구별할 수 없다.
        """
        if frame.command not in (CMD_ON, CMD_OFF) or not frame.is_state_report:
            return
        room  = self._room(frame.dev_room)
        state = 'on' if frame.command == CMD_ON else 'off'
        await self._notify(f'kocom/{room}/gas/state', {'state': state})

    async def _pub_elevator(self, frame: PacketFrame) -> None:
        """엘리베이터 호출·도착 이벤트.

        실측 결과 이벤트는 커맨드가 아니라 payload[0]으로 구분된다.
          cmd 0x01 + payload[0]=0x00 → 호출  (현관 버튼: 0x44 → 월패드)
          cmd 0x01 + payload[0]=0x03 → 도착  (월패드 → 0x44)
          cmd 0x00                   → 미지원 프레임 (12/12 무응답)

        커맨드와 방향만으로 판정하면 도착을 호출로 오독한다. 도착 통보는
        월패드가 보내므로 '장치 발신만 신뢰' 규칙의 예외로 둔다.

        월패드 화면에서 호출한 경우는 버스에 프레임이 남지 않아 감지할 수 없다.
        """
        if frame.command != CMD_ON or frame.is_ack:
            return

        event = ELEVATOR_EVENT.get(frame.payload[0])
        if event is None:
            log.debug('[Elevator] Unknown payload[0]=0x%02x', frame.payload[0])
            return

        state: dict = {
            'state':     'on' if event == 'called' else 'off',
            'direction': event,
        }

        # 층 정보: 이 세대 엘리베이터는 싣지 않지만(항상 00 00), 다른 세대를
        # 위해 값이 있을 때만 해석한다.
        b1, b2 = frame.payload[1], frame.payload[2]
        if b1 != 0x00:
            if b2 != 0x00:
                state['floor'] = f'{chr(b1)}{chr(b2)}'
            elif b1 >> 4 == 0x08:
                state['floor'] = f'B{b1 & 0x0F}'
            else:
                state['floor'] = str(b1)

            rs485_floor = int(self._config.get('Elevator', 'rs485_floor', fallback='0'))
            if rs485_floor != 0:
                try:
                    if int(state['floor']) == rs485_floor:
                        state['state'] = 'off'
                        state['direction'] = 'arrival'
                except ValueError:
                    pass

        await self._notify('kocom/myhome/elevator/state', state)

    async def _pub_motion(self, frame: PacketFrame) -> None:
        """현관 방범 유닛(0x60)의 두 가지 프레임.

          cmd 0x04 → 동작 감지 보고. payload[0] 0x01 감지 / 0x00 해제
          cmd 0x00 → 경비(외출모드) 설정. payload[0] 0xFF 활성 / 0x00 해제

        기존 구현은 cmd 0x00을 '감지 없음'으로 읽었으나 실제로는 외출모드
        설정이다. 그대로 두면 외출모드 진입을 '동작 없음'으로 잘못 보고한다.
        """
        room = self._room(frame.dev_room)

        if frame.command == CMD_MOTION:
            if not frame.is_state_report:
                return
            state = 'on' if frame.payload[0] == 0x01 else 'off'
            await self._notify(f'kocom/{room}/motion/state', {'state': state})
            return

        if frame.command != CMD_STATE:
            return

        # 경비 설정은 세 가지 경로로 관측된다.
        #   1) 현관 스위치  : 0x60 → 월패드 0x0B 보고
        #   2) 월패드 조작  : 월패드 → 0x60 0x0B 명령
        #   3) 애드온 조작  : 우리 TX는 버스에서 되돌아오지 않지만, 0x60이
        #                     0x0D ACK를 보내므로 그것이 실제 확인이 된다
        # ACK는 명령의 반향이므로 payload가 곧 적용된 값이다.
        accept = frame.packet_type == PT_SEND or (frame.is_ack and not frame.from_wallpad)
        if not accept:
            return
        away = 'on' if frame.payload[0] == 0xFF else 'off'
        await self._notify('kocom/myhome/away/state', {'state': away})

    async def _pub_airquality(self, frame: PacketFrame) -> None:
        if frame.command not in (CMD_STATE, CMD_QUERY) or not frame.is_state_report:
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

    async def _pub_time(self, frame: PacketFrame, offset: int, with_seconds: bool) -> None:
        """월패드가 알려주는 시각.

        두 곳에서 얻을 수 있다.
          0x86 cmd 0x01 : payload = YY MM DD HH MM SS   (offset 0, 초 포함)
          cmd 0x3B      : payload = 00 YY MM DD HH MM   (offset 1, 초 없음)
        값은 BCD가 아니라 그대로의 수다.

        이 프레임은 월패드가 스스로 보내는 것이므로 **버스가 살아 있다는 가장
        좋은 증거**다. 시각과 시스템 시각의 오차를 함께 발행해 HA에서 월패드·
        EW11 상태를 확인할 수 있게 한다.
        """
        if frame.command == CMD_TIME_REQ:
            return   # 시각 요청 프레임에는 값이 없다
        pl = frame.payload
        try:
            yy, mm, dd, hh, mi = pl[offset:offset + 5]
            ss = pl[offset + 5] if with_seconds and len(pl) > offset + 5 else 0
            wallpad_dt = datetime(2000 + yy, mm, dd, hh, mi, ss)
        except (ValueError, IndexError):
            log.debug('[Time] Unparseable time payload: %s', pl.hex())
            return

        drift = (datetime.now() - wallpad_dt).total_seconds()
        self.diag['last_wallpad_time'] = wallpad_dt
        self.diag['last_wallpad_time_rx'] = time.time()

        await self._notify('kocom/myhome/wallpad/time', {
            'time':   wallpad_dt.isoformat(sep=' '),
            'drift':  round(drift, 1),
            'source': 'broadcast' if frame.dev_type == 'timesync' else 'thermostat',
        })

    # ── 진단 ────────────────────────────────────────────────────
    def diagnostics(self) -> dict:
        """버스 건강 상태 스냅샷. kocom.py가 주기적으로 발행한다."""
        d = self.diag
        now = time.time()
        return {
            'rx_total':   d['rx_total'],
            'rx_invalid': d['rx_invalid'],
            'rx_retry':   d['rx_retry'],
            'tx_total':   d['tx_total'],
            'error_rate': (round(d['rx_invalid'] / d['rx_total'] * 100, 2)
                           if d['rx_total'] else 0.0),
            'last_rx_age': (round(now - d['last_rx'], 1) if d['last_rx'] else None),
            'wallpad_time': (d['last_wallpad_time'].isoformat(sep=' ')
                             if d['last_wallpad_time'] else None),
            'wallpad_time_age': (round(now - d['last_wallpad_time_rx'], 1)
                                 if d['last_wallpad_time_rx'] else None),
        }

    # ── 패킷 생성 ────────────────────────────────────────────────
    @staticmethod
    def _make_packet(
        dest_dev: int, dest_room: int,
        src_dev:  int, src_room:  int,
        command:  int, data: bytes,
        ptype:    int = PT_SEND,
    ) -> bytes:
        """21바이트 송신 패킷 생성.

        byte3 = 상위 니블(패킷 타입) + 하위 니블(시퀀스). 최초 전송이므로
        시퀀스는 항상 0xC다. 전체 소등처럼 방 코드 0xFF를 쓰는 브로드캐스트는
        ptype=PT_BROADCAST(0x09)로 보내야 월패드와 같은 형태가 된다.
        """
        body = (
            bytes([0x30, (ptype << 4) | SEQ_FIRST, 0x00])
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
        if dev == 'lightcutoff':
            return [self._build_cutoff(action == 'on')]
        if dev == 'away':
            return self.build_away(action == 'on')
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

        # 방 전체 켜기/끄기는 8바이트를 모두 채워 한 프레임으로 보낸다.
        # 월패드가 쓰는 와일드카드이며, 장치가 실제 채널 수만 반영해 응답한다.
        if kwargs.get('index') == 'all' or action in ('all_on', 'all_off'):
            fill = 0xFF if action in ('on', 'all_on') else 0x00
            return [self._make_packet(dest_dev, dest_room, 0x01, 0x00,
                                      CMD_STATE, bytes([fill] * 8))]

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
        elif action == 'away':
            # 실측: 명령은 모드(byte0)와 외출 여부(byte1)만 싣고 나머지는 비운다.
            # 외출을 켜면 장치가 설정온도를 10°C로 스스로 바꾸고, 끄면 되돌린다.
            # 애드온이 온도를 함께 지정하면 그 동작과 충돌하므로 비워 둔다.
            data[0] = 0x11
            # HA climate은 preset 이름('away' / 'none')을 보낸다.
            # 'on'/'true'도 받아 MQTT로 직접 제어하는 경우를 함께 지원한다.
            want = str(kwargs.get('away', '')).lower()
            data[1] = 0x01 if want in ('away', 'on', 'true', '1') else 0x00
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
            if action == 'on':
                data[0] = 0x11
                data[1] = VENT_PRESET_CODE.get('auto', 0x02)
                data[2] = 0x00
            else:  # off
                data[0] = 0x00
                data[2] = (cur_speed & 0xF0) | (cur_timer & 0x0F)

        return self._make_packet(dest_dev, dest_room, 0x01, 0x00, 0x00, bytes(data))

    def _build_gas(self, room: str) -> bytes:
        """가스밸브 차단 패킷 (off 전용)."""
        dest_dev, dest_room = CODE_DEVICE['gas'], ROOM_CODE.get(room, 0x00)
        return self._make_packet(dest_dev, dest_room, 0x01, 0x00, 0x02, bytes(8))

    def _build_elevator(self) -> bytes:
        """엘리베이터 호출 패킷.

        실측한 현관 버튼 호출과 같은 형태다 — 0x44가 월패드에 cmd 0x01을 보내고
        payload[0]=0x00(호출)이다. 장치 발신 프레임을 애드온이 대신 만드는
        방식이라 실제 동작은 세대에 따라 다를 수 있다.
        """
        return self._make_packet(0x01, 0x00, CODE_DEVICE['elevator'], 0x00, CMD_ON, bytes(8))

    def _build_cutoff(self, on: bool) -> bytes:
        """전체 소등 / 해제 패킷.

        방 코드 0xFF 대상 브로드캐스트이며 패킷 타입이 0x09다. 해제 페이로드는
        FF×8이지만 '전부 켜기'가 아니라 **소등 직전 상태 복원**을 뜻한다.
        """
        cmd  = CMD_CUTOFF_ON if on else CMD_CUTOFF_OFF
        data = bytes(8) if on else bytes([0xFF] * 8)
        return self._make_packet(CODE_DEVICE['light'], 0xFF, 0x01, 0x00,
                                 cmd, data, ptype=PT_BROADCAST)

    def build_away(self, on: bool) -> List[bytes]:
        """외출모드 진입/해제.

        두 가지 방식이 있고 실측으로 동작이 확인된 쪽이 다르다.

        entrance (기본) — 현관 스위치와 같은 방식.
            현관 스위치는 매크로를 직접 실행하지 않는다. `0x60`이 월패드에
            경비 활성을 **보고**하고, 그러면 **월패드가 스스로** 가스 차단·
            보조 차단·엘리베이터 호출·전체 소등을 순서대로 실행한다.
            따라서 프레임 하나만 보내면 된다. 월패드 자신의 상태도 함께
            바뀌므로 월패드 화면에도 외출모드로 표시된다.

        macro — 애드온이 각 장치에 직접 명령한다.
            소등·가스 차단은 되지만 **월패드는 외출모드로 전환되지 않는다**
            (월패드는 마스터이므로 명령을 받지 않는다). entrance 방식이
            동작하지 않는 세대를 위한 대비책으로 남겨 둔다.
        """
        mode = self._config.get('Away', 'mode', fallback='entrance')
        if mode != 'macro':
            return [self._build_away_notify(on)]

        gas_room = ROOM_CODE.get(
            self._config.get('Away', 'gas_room', fallback='livingroom'), 0x00)

        if not on:
            return [
                self._build_cutoff(False),
                self._make_packet(CODE_DEVICE['motion'], 0x00, 0x01, 0x00,
                                  CMD_STATE, bytes(8)),
            ]

        return [
            self._make_packet(CODE_DEVICE['gas'], gas_room, 0x01, 0x00,
                              CMD_OFF, bytes(8)),
            self._make_packet(CODE_DEVICE['shutoff2'], 0x00, 0x01, 0x00,
                              CMD_OFF, bytes(8)),
            self._make_packet(CODE_DEVICE['motion'], 0x00, 0x01, 0x00,
                              CMD_STATE, bytes([0xFF] + [0x00] * 7)),
            self._build_cutoff(True),
        ]

    def _build_away_notify(self, on: bool) -> bytes:
        """현관 방범 유닛(0x60)이 월패드에 경비 설정을 보고하는 프레임.

        실측한 현관 스위치 조작과 같은 형태다 (2026-09-26 14:44:01).
        이 프레임을 받으면 월패드가 나머지 동작을 스스로 수행한다.
        """
        data = bytes([0xFF] + [0x00] * 7) if on else bytes(8)
        return self._make_packet(0x01, 0x00, CODE_DEVICE['motion'], 0x00,
                                 CMD_STATE, data)
