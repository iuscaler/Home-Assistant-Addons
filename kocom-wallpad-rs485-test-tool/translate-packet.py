#!/usr/bin/env python3
"""
Kocom Wallpad RS485 패킷 해석기

stdin으로 16진수 패킷을 입력하면 사람이 읽기 쉬운 형태로 출력합니다.

사용법:
  echo "AA 55 30 BC 00 0E 00 01 00 3A 00 00 00 00 00 00 00 00 35 0D 0D" | python translate-packet.py
  echo "AA5530BC000E0001003A0000000000000000350D0D" | python translate-packet.py
  python translate-packet.py    (대화형: 한 줄씩 입력)

# 로 시작하는 줄은 주석으로 무시합니다.
"""

from __future__ import annotations

import re
import sys

# ── 패킷 구조 ─────────────────────────────────────────────────────────
#
#  Byte  0-1 : AA 55            prefix
#  Byte  2   : 0x30             fixed
#  Byte  3   : upper-nibble = packet_type (0x0B=명령/조회, 0x0D=응답/ACK)
#  Byte  4   : 0x00             fixed
#  Byte  5   : dest device code
#  Byte  6   : dest room code
#  Byte  7   : src  device code
#  Byte  8   : src  room code
#  Byte  9   : command byte
#  Byte 10-17: payload (8 bytes)
#  Byte 18   : checksum = sum(raw[2:18]) % 256
#  Byte 19-20: 0D 0D            suffix
#
PACKET_PREFIX = bytes([0xAA, 0x55])
PACKET_SUFFIX = bytes([0x0D, 0x0D])
PACKET_LEN    = 21

# ── 코드 매핑 ─────────────────────────────────────────────────────────
DEVICE_NAME: dict[int, str] = {
    0x01: '월패드',
    0x0E: '조명',
    0x3B: '콘센트',
    0x36: '온도조절기',
    0x39: '에어컨',
    0x48: '환기장치',
    0x2C: '가스밸브',
    0x44: '엘리베이터',
    0x60: '동작감지기',
    0x98: '공기질센서',
}

ROOM_NAME: dict[int, str] = {
    0x00: '거실',
    0x01: '방1',
    0x02: '방2',
    0x03: '방3',
    0x04: '주방',
    0xFF: '전체',
}

PACKET_TYPE_NAME: dict[int, str] = {
    0x0B: '명령/조회',
    0x0D: '응답/ACK',
}

AIRCON_HVAC_NAME: dict[int, str] = {
    0x00: 'cool(냉방)', 0x01: 'fan_only(송풍)',
    0x02: 'dry(제습)',  0x03: 'auto(자동)',
}
AIRCON_FAN_NAME: dict[int, str] = {
    0x01: 'low(약풍)', 0x02: 'medium(중풍)',
    0x03: 'high(강풍)', 0x04: 'auto(자동)',
}
VENT_PRESET_NAME: dict[int, str] = {
    0x00: 'unknown',           0x01: 'ventilation(환기)',
    0x02: 'auto(자동)',        0x03: 'bypass(바이패스)',
    0x05: 'sleep(취침)',       0x08: 'air purification(공기청정)',
}
VENT_SPEED_NAME: dict[int, str] = {
    0x40: '약풍(low)', 0x80: '중풍(medium)', 0xC0: '강풍(high)',
}
ELEVATOR_DIR_NAME: dict[int, str] = {
    0x00: '대기', 0x01: '하강', 0x02: '상승', 0x03: '도착',
}
COMMAND_NAME: dict[int, str] = {
    0x00: '상태/제어',
    0x01: '열림(가스) / 호출(엘리베이터)',
    0x02: '차단(가스)',
    0x04: '감지됨(동작)',
    0x3A: '조회',
    0x65: '조명차단 ON',
}


# ── 헬퍼 ─────────────────────────────────────────────────────────────
def _hex(data: bytes) -> str:
    return ' '.join(f'{b:02X}' for b in data)


def _dev_label(code: int) -> str:
    name = DEVICE_NAME.get(code)
    return f'{name}(0x{code:02X})' if name else f'알수없음(0x{code:02X})'


def _room_label(code: int) -> str:
    name = ROOM_NAME.get(code)
    return f'{name}(0x{code:02X})' if name else f'0x{code:02X}'


def parse_hex_input(text: str) -> bytes | None:
    """공백·하이픈·콜론으로 구분하거나 연속된 16진수 문자열을 bytes로 변환."""
    clean = re.sub(r'[\s\-:]', '', text).upper()
    if not re.fullmatch(r'[0-9A-F]*', clean) or len(clean) % 2 != 0:
        return None
    return bytes.fromhex(clean)


# ── 장치별 페이로드 해석 ───────────────────────────────────────────────
def _decode_payload(
    dev_code: int,
    dev_room: int,
    command: int,
    payload: bytes,
    packet_type: int,
    from_wallpad: bool,
) -> list[str]:
    """장치 코드와 커맨드에 따라 페이로드를 사람이 읽을 수 있는 문자열 목록으로 변환."""
    dev_type = {
        0x0E: 'light',    0x3B: 'outlet',  0x36: 'thermo',
        0x39: 'aircon',   0x48: 'fan',     0x2C: 'gas',
        0x44: 'elevator', 0x60: 'motion',  0x98: 'airquality',
    }.get(dev_code)

    room  = ROOM_NAME.get(dev_room, f'방{dev_room}')
    lines: list[str] = []

    # 조명 차단기 (light 장치이면서 room = 0xFF)
    if dev_type == 'light' and dev_room == 0xFF:
        state = 'ON' if command == 0x65 else 'OFF'
        lines.append(f'조명 차단기 전체: {state}')
        return lines

    if dev_type in ('light', 'outlet'):
        label = '조명' if dev_type == 'light' else '콘센트'
        if command == 0x3A:
            lines.append(f'{label} ({room}) 상태 조회')
        elif command == 0x00:
            direction_hint = '월패드 제어 명령' if from_wallpad else '장치 상태 응답'
            switch_states = [
                f'#{i + 1} {"ON " if payload[i] == 0xFF else "off"}' for i in range(8)
            ]
            active = [f'#{i + 1}' for i in range(8) if payload[i] == 0xFF]
            lines.append(f'{label} ({room})  [{direction_hint}]')
            lines.append(f'  켜진 채널: {", ".join(active) if active else "없음 (전부 꺼짐)"}')
            lines.append(f'  채널 상태: {" | ".join(switch_states)}')
        else:
            lines.append(f'{label} ({room}): 알 수 없는 커맨드 0x{command:02X}')

    elif dev_type == 'thermo':
        idx = dev_room
        if command == 0x3A:
            lines.append(f'온도조절기 #{idx} 상태 조회')
        elif command == 0x00:
            heat_mode = '난방' if (payload[0] >> 4) == 0x01 else '꺼짐'
            away      = '외출 중' if (payload[1] & 0x0F) == 0x01 else '일반'
            set_temp  = float(payload[2])
            hot_temp  = payload[3]
            cur_temp  = float(payload[4])
            heat_temp = payload[5]
            error     = payload[6]
            lines.append(f'온도조절기 #{idx}: 모드={heat_mode}  상태={away}')
            lines.append(f'  현재온도: {cur_temp}°C  |  설정온도: {set_temp}°C')
            if hot_temp > 0:
                lines.append(f'  온수온도: {hot_temp}°C')
            if heat_temp > 0:
                lines.append(f'  난방온도: {heat_temp}°C')
            if error != 0:
                lines.append(f'  에러코드: 0x{error:02X}')
        else:
            lines.append(f'온도조절기 #{dev_room}: 알 수 없는 커맨드 0x{command:02X}')

    elif dev_type == 'aircon':
        if command == 0x3A:
            lines.append(f'에어컨 ({room}) 상태 조회')
        elif command == 0x00:
            on    = payload[0] == 0x10
            hvac  = AIRCON_HVAC_NAME.get(payload[1], f'0x{payload[1]:02X}') if on else '-'
            fan   = AIRCON_FAN_NAME.get(payload[2], f'0x{payload[2]:02X}')
            cur_t = float(payload[4])
            set_t = float(payload[5])
            lines.append(f'에어컨 ({room}): {"운전 중" if on else "꺼짐"}')
            if on:
                lines.append(f'  운전모드: {hvac}  |  팬: {fan}')
            lines.append(f'  현재온도: {cur_t}°C  |  설정온도: {set_t}°C')
        else:
            lines.append(f'에어컨 ({room}): 알 수 없는 커맨드 0x{command:02X}')

    elif dev_type == 'fan':
        if command == 0x3A:
            lines.append(f'환기장치 ({room}) 상태 조회')
        elif command == 0x00:
            on          = (payload[0] >> 4) == 0x01
            preset      = VENT_PRESET_NAME.get(payload[1], f'0x{payload[1]:02X}')
            speed_code  = payload[2] & 0xF0
            timer_hours = payload[2] & 0x0F
            speed_label = VENT_SPEED_NAME.get(speed_code, f'0x{speed_code:02X}')
            co2         = (payload[4] * 100) + payload[5]
            error       = payload[6]
            lines.append(f'환기장치 ({room}): {"운전 중" if on else "꺼짐"}')
            if on:
                lines.append(f'  모드: {preset}  |  풍량: {speed_label}')
                if timer_hours > 0:
                    lines.append(f'  타이머: {timer_hours}시간 후 꺼짐')
            if co2 > 0:
                lines.append(f'  CO2: {co2} ppm')
            if error != 0:
                lines.append(f'  에러코드: 0x{error:02X}')
        else:
            lines.append(f'환기장치 ({room}): 알 수 없는 커맨드 0x{command:02X}')

    elif dev_type == 'gas':
        if command == 0x3A:
            lines.append(f'가스밸브 ({room}) 상태 조회')
        elif command == 0x01:
            lines.append(f'가스밸브 ({room}): 열림 (ON)')
        elif command == 0x02:
            lines.append(f'가스밸브 ({room}): 잠김 (OFF)')
        else:
            lines.append(f'가스밸브 ({room}): 알 수 없는 커맨드 0x{command:02X}')

    elif dev_type == 'elevator':
        if command == 0x01 and from_wallpad:
            lines.append('엘리베이터 호출 (월패드 → RS485)')
        else:
            direction = (
                '호출됨'
                if payload[0] == 0x00 and packet_type == 0x0D
                else ELEVATOR_DIR_NAME.get(payload[0], f'0x{payload[0]:02X}')
            )
            b1, b2 = payload[1], payload[2]
            floor = '알 수 없음'
            if b1 != 0x00:
                if b2 != 0x00:
                    try:
                        floor = f'{chr(b1)}{chr(b2)}층'
                    except (ValueError, OverflowError):
                        floor = f'{b1:02X}{b2:02X}'
                elif b1 >> 4 == 0x08:
                    floor = f'B{b1 & 0x0F}층'
                else:
                    floor = f'{b1}층'
            lines.append(f'엘리베이터: 방향={direction}  현재층={floor}')

    elif dev_type == 'motion':
        if command == 0x04:
            lines.append(f'동작감지기 ({room}): 동작 감지됨')
        elif command == 0x00:
            lines.append(f'동작감지기 ({room}): 감지 없음')
        else:
            lines.append(f'동작감지기 ({room}): 알 수 없는 커맨드 0x{command:02X}')

    elif dev_type == 'airquality':
        if command in (0x00, 0x3A):
            pm10     = payload[0]
            pm25     = payload[1]
            co2      = int.from_bytes(payload[2:4], 'big')
            voc      = int.from_bytes(payload[4:6], 'big')
            temp     = payload[6]
            humidity = payload[7]
            lines.append(f'공기질센서 ({room}):')
            lines.append(f'  PM10: {pm10} μg/m³  |  PM2.5: {pm25} μg/m³')
            lines.append(f'  CO2:  {co2} ppm      |  VOC:   {voc} ppb')
            lines.append(f'  온도: {temp}°C        |  습도:  {humidity}%')
        else:
            lines.append(f'공기질센서 ({room}): 알 수 없는 커맨드 0x{command:02X}')

    else:
        lines.append(f'알 수 없는 장치 코드 0x{dev_code:02X}: 커맨드 0x{command:02X}')
        lines.append(f'  페이로드 raw: {_hex(payload)}')

    return lines


# ── 패킷 번역 ─────────────────────────────────────────────────────────
def translate(raw: bytes) -> str:
    W  = 66
    eq = '═' * W
    da = '─' * W
    out: list[str] = []

    out.append(f'╔{eq}╗')
    out.append(f'║  패킷 : {_hex(raw)}')
    out.append(f'╠{eq}╣')

    if len(raw) != PACKET_LEN:
        out.append(f'║  오류: {len(raw)}바이트 (예상: {PACKET_LEN}바이트)')
        out.append(f'╚{eq}╝')
        return '\n'.join(out)

    # 유효성 검사
    prefix_ok  = raw[:2] == PACKET_PREFIX
    suffix_ok  = raw[-2:] == PACKET_SUFFIX
    chk_calc   = sum(raw[2:18]) % 256
    chk_stored = raw[18]
    chk_ok     = chk_calc == chk_stored
    valid      = prefix_ok and suffix_ok and chk_ok

    status = '✓ 유효' if valid else '✗ 오류'
    if not prefix_ok:
        status += '  [프리픽스 불일치]'
    if not suffix_ok:
        status += '  [서픽스 불일치]'
    if not chk_ok:
        status += f'  [체크섬: 계산=0x{chk_calc:02X} 저장=0x{chk_stored:02X}]'

    out.append(f'║  상태 : {status}')
    out.append(f'╠{eq}╣')

    # 필드 파싱
    ptype_raw   = (raw[3] >> 4) & 0x0F
    dest_dev    = raw[5]
    dest_room   = raw[6]
    src_dev     = raw[7]
    src_room    = raw[8]
    command     = raw[9]
    payload     = raw[10:18]

    ptype_label = PACKET_TYPE_NAME.get(ptype_raw, f'알수없음(0x{ptype_raw:X})')
    cmd_label   = COMMAND_NAME.get(command, '')

    from_wallpad = (src_dev == 0x01)
    if src_dev == 0x01:
        direction = f'월패드 → {_dev_label(dest_dev)} ({_room_label(dest_room)})'
    elif dest_dev == 0x01:
        direction = f'{_dev_label(src_dev)} ({_room_label(src_room)}) → 월패드'
    else:
        direction = f'{_dev_label(src_dev)} → {_dev_label(dest_dev)}'

    # peer: 월패드가 아닌 쪽 장치
    if dest_dev == 0x01:
        peer_dev, peer_room = src_dev, src_room
    else:
        peer_dev, peer_room = dest_dev, dest_room

    def row(label: str, value: str) -> None:
        out.append(f'║  {label:<10} {value}')

    row('패킷타입 :', f'0x{ptype_raw:02X}  {ptype_label}')
    row('수신지   :', f'{_dev_label(dest_dev)}  방={_room_label(dest_room)}')
    row('발신지   :', f'{_dev_label(src_dev)}  방={_room_label(src_room)}')
    row('방향     :', direction)
    out.append(f'║  {da}')
    row('커맨드   :', f'0x{command:02X}  {cmd_label}')
    row('페이로드 :', _hex(payload))
    row('체크섬   :', f'0x{chk_stored:02X}  (계산값: 0x{chk_calc:02X})  {"✓" if chk_ok else "✗ 불일치"}')

    # 장치별 해석
    out.append(f'╠{eq}╣')
    interp = _decode_payload(peer_dev, peer_room, command, payload, ptype_raw, from_wallpad)
    for line in interp:
        out.append(f'║  {line}')
    out.append(f'╚{eq}╝')

    return '\n'.join(out)


# ── 진입점 ──────────────────────────────────────────────────────────
def main() -> None:
    if sys.stdin.isatty():
        print('Kocom Wallpad RS485 패킷 해석기')
        print('16진수 패킷을 입력하세요 (공백/하이픈 구분 모두 가능). 종료: Ctrl+D / Ctrl+C')
        print()

    for line in sys.stdin:
        line = line.strip()
        if not line or line.startswith('#'):
            continue

        raw = parse_hex_input(line)
        if raw is None:
            print(f'오류: 올바른 16진수가 아닙니다 → {line!r}')
            print()
            continue

        # 21바이트보다 길면 버스 스트림으로 간주하고 패킷을 순서대로 추출
        if len(raw) > PACKET_LEN:
            extracted = False
            while True:
                idx = raw.find(PACKET_PREFIX)
                if idx < 0 or len(raw) - idx < PACKET_LEN:
                    break
                raw = raw[idx:]
                print(translate(raw[:PACKET_LEN]))
                print()
                raw = raw[PACKET_LEN:]
                extracted = True
            if not extracted:
                print('오류: AA 55 프리픽스를 찾을 수 없습니다.')
                print()
        else:
            print(translate(raw))
            print()


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\n종료합니다.')
