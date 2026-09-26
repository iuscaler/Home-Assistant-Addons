#!/usr/bin/env python3
"""
Kocom Wallpad RS485 패킷 로거

RS485 시리얼 또는 TCP 소켓으로 수신되는 모든 패킷을 캡처하여 터미널에 출력한다.
패킷은 AA 55로 시작하고 0D 0D로 끝나는 21바이트 고정 길이다.
같은 폴더의 translate-packet.py 를 불러와 해석 결과도 함께 출력한다.

사용법:
  # 소켓 연결 (기본) — 요약 해석 포함
  python packet-log.py --type socket --host 192.168.1.100 --port 8899

  # 시리얼 연결
  python packet-log.py --type serial --serial-port /dev/ttyUSB0 --baud 9600

  # 해석 상세도 선택 (off = 16진수만, short = 요약, full = 전체 박스)
  python packet-log.py --decode full

  # 로그를 파일로 저장 (화면 출력은 그대로 유지)
  python packet-log.py --log capture.log --raw-log capture.hex
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO

# ── 패킷 상수 (kocom-wallpad-rs485/const.py와 동일) ─────────────────
PACKET_PREFIX = bytes([0xAA, 0x55])
PACKET_SUFFIX = bytes([0x0D, 0x0D])
PACKET_LEN    = 21

# ── 로그 파일 핸들 (해당 옵션 지정 시에만 열림) ──────────────────────
_LOG_FH: TextIO | None = None   # 화면에 출력되는 내용 그대로
_RAW_FH: TextIO | None = None   # 16진수 한 줄씩 (translate-packet.py 재입력용)
_CSV_FH: TextIO | None = None   # "ISO시각,16진수" (analyze-log.py 분석용)


def out(msg: str = '') -> None:
    """터미널과 로그 파일에 동시에 출력한다."""
    print(msg, flush=True)
    if _LOG_FH is not None:
        _LOG_FH.write(msg + '\n')
        _LOG_FH.flush()


def _hex(data: bytes) -> str:
    return ' '.join(f'{b:02X}' for b in data)


# ── translate-packet.py 동적 로드 ────────────────────────────────────
def _load_translator() -> Any | None:
    """파일명에 하이픈이 있어 일반 import가 불가능하므로 경로로 직접 로드한다."""
    path = Path(__file__).resolve().with_name('translate-packet.py')
    if not path.exists():
        return None
    try:
        spec = importlib.util.spec_from_file_location('translate_packet', path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception as e:  # 해석기 문제로 로거가 죽지 않도록
        print(f'경고: translate-packet.py 로드 실패 ({e}) — 16진수만 출력합니다.')
        return None


class PacketParser:
    """스트림 바이트를 받아 완성된 패킷을 추출하고 출력한다."""

    def __init__(self, decode: str = 'short', translator: Any | None = None) -> None:
        self._buf: bytearray = bytearray()
        self._count: int = 0
        self._decode = decode if translator is not None else 'off'
        self._tr = translator

    def feed(self, data: bytes) -> None:
        self._buf.extend(data)
        self._extract()

    def _extract(self) -> None:
        while True:
            # AA 55 시작 위치 탐색
            idx = self._buf.find(PACKET_PREFIX)
            if idx == -1:
                if self._buf:
                    out(f'  [noise {len(self._buf)}B] {_hex(bytes(self._buf))}')
                self._buf.clear()
                return

            # 시작 위치 이전 바이트는 노이즈로 처리
            if idx > 0:
                noise = bytes(self._buf[:idx])
                out(f'  [noise {len(noise)}B] {_hex(noise)}')
                del self._buf[:idx]

            # 패킷 전체가 버퍼에 도착할 때까지 대기
            if len(self._buf) < PACKET_LEN:
                return

            packet = bytes(self._buf[:PACKET_LEN])

            # suffix 검증 — 맞지 않으면 prefix 1바이트를 버리고 재탐색
            if not packet.endswith(PACKET_SUFFIX):
                out(f'  [bad suffix] {_hex(packet)}')
                del self._buf[0]
                continue

            del self._buf[:PACKET_LEN]
            self._print(packet)

    # ── 출력 ────────────────────────────────────────────────────────
    def _print(self, packet: bytes) -> None:
        self._count += 1
        now = datetime.now()
        header = f'[{now.strftime("%H:%M:%S.%f")[:-3]}] #{self._count:04d}'
        out(f'{header}  {_hex(packet)}')

        if _RAW_FH is not None:
            _RAW_FH.write(_hex(packet) + '\n')
            _RAW_FH.flush()

        if _CSV_FH is not None:
            _CSV_FH.write(f'{now.isoformat()},{packet.hex().upper()}\n')
            _CSV_FH.flush()

        if self._decode == 'off':
            return

        indent = ' ' * (len(header) + 2)
        try:
            if self._decode == 'full':
                for line in self._tr.translate(packet).splitlines():
                    out(indent + line)
            else:
                for line in self._summarize(packet):
                    out(indent + line)
        except Exception as e:  # 해석 실패해도 캡처는 계속
            out(f'{indent}[해석 오류] {e}')

    def _summarize(self, packet: bytes) -> list[str]:
        """translate-packet.py의 해석 로직으로 요약 몇 줄을 만든다."""
        tr = self._tr
        ptype     = (packet[3] >> 4) & 0x0F
        dest_dev  = packet[5]
        dest_room = packet[6]
        src_dev   = packet[7]
        src_room  = packet[8]
        command   = packet[9]
        payload   = packet[10:18]

        from_wallpad = src_dev == 0x01
        if from_wallpad:
            direction = f'월패드 → {tr._dev_label(dest_dev)} ({tr._room_label(dest_room)})'
        elif dest_dev == 0x01:
            direction = f'{tr._dev_label(src_dev)} ({tr._room_label(src_room)}) → 월패드'
        else:
            direction = f'{tr._dev_label(src_dev)} → {tr._dev_label(dest_dev)}'

        ptype_label = tr.PACKET_TYPE_NAME.get(ptype, f'알수없음(0x{ptype:X})')
        cmd_label   = tr.COMMAND_NAME.get(command, '')
        cmd_text    = f'cmd=0x{command:02X}' + (f' {cmd_label}' if cmd_label else '')

        chk_calc = sum(packet[2:18]) % 256
        chk_flag = '' if chk_calc == packet[18] else f'  ✗ 체크섬 불일치(계산=0x{chk_calc:02X})'

        lines = [f'{direction} | {ptype_label} | {cmd_text}{chk_flag}']

        # 월패드가 아닌 쪽 장치를 기준으로 페이로드 해석
        if dest_dev == 0x01:
            peer_dev, peer_room = src_dev, src_room
        else:
            peer_dev, peer_room = dest_dev, dest_room

        lines += tr._decode_payload(
            peer_dev, peer_room, command, payload, ptype, from_wallpad
        )
        return lines


# ── 연결별 수신 루프 ─────────────────────────────────────────────────

async def _recv_loop(reader: asyncio.StreamReader, parser: PacketParser) -> None:
    while True:
        chunk = await reader.read(512)
        if not chunk:
            raise ConnectionResetError('연결이 끊겼습니다.')
        parser.feed(chunk)


async def run_socket(host: str, port: int, parser: PacketParser) -> None:
    out(f'[소켓] {host}:{port} 연결 중...')

    reconnect_delay = 5.0
    while True:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=10.0
            )
            out(f'[소켓] 연결됨 — 패킷 캡처 시작')
            reconnect_delay = 5.0
            try:
                await _recv_loop(reader, parser)
            finally:
                writer.close()
        except asyncio.TimeoutError:
            out(f'[소켓] 연결 시간 초과. {reconnect_delay:.0f}초 후 재시도...')
        except (ConnectionResetError, OSError) as e:
            out(f'[소켓] {e}. {reconnect_delay:.0f}초 후 재시도...')

        await asyncio.sleep(reconnect_delay)
        reconnect_delay = min(reconnect_delay * 2, 60.0)
        out(f'[소켓] {host}:{port} 재연결 중...')


async def run_serial(serial_port: str, baud: int, parser: PacketParser) -> None:
    try:
        import serial_asyncio  # type: ignore
    except ImportError:
        out('오류: serial_asyncio 패키지가 없습니다. pip install pyserial-asyncio 실행 후 다시 시도하세요.')
        sys.exit(1)

    out(f'[시리얼] {serial_port} @ {baud}bps 연결 중...')

    reconnect_delay = 5.0
    while True:
        try:
            reader, writer = await serial_asyncio.open_serial_connection(
                url=serial_port, baudrate=baud
            )
            out(f'[시리얼] 연결됨 — 패킷 캡처 시작')
            reconnect_delay = 5.0
            try:
                await _recv_loop(reader, parser)
            finally:
                writer.close()
        except (OSError, Exception) as e:
            out(f'[시리얼] {e}. {reconnect_delay:.0f}초 후 재시도...')

        await asyncio.sleep(reconnect_delay)
        reconnect_delay = min(reconnect_delay * 2, 60.0)
        out(f'[시리얼] {serial_port} 재연결 중...')


# ── 진입점 ──────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description='Kocom Wallpad RS485 패킷 로거',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            '예시:\n'
            '  python packet-log.py --type socket --host 192.168.1.100 --port 8899\n'
            '  python packet-log.py --type serial --serial-port /dev/ttyUSB0\n'
            '  python packet-log.py --decode full --log capture.log\n'
            '  python packet-log.py --raw-log capture.hex   # 나중에 재해석용\n'
        ),
    )
    p.add_argument('--type', choices=['socket', 'serial'], default='socket',
                   help='연결 방식 (기본: socket)')
    p.add_argument('--host', default='192.168.1.100',
                   help='소켓 서버 주소 (기본: 192.168.1.100)')
    p.add_argument('--port', type=int, default=8899,
                   help='소켓 포트 (기본: 8899)')
    p.add_argument('--serial-port', default='/dev/ttyUSB0',
                   help='시리얼 포트 경로 (기본: /dev/ttyUSB0)')
    p.add_argument('--baud', type=int, default=9600,
                   help='시리얼 보드레이트 (기본: 9600)')
    p.add_argument('--decode', choices=['off', 'short', 'full'], default='short',
                   help='해석 상세도: off=16진수만, short=요약(기본), full=전체 박스')
    p.add_argument('--log', metavar='PATH',
                   help='화면 출력 내용을 그대로 저장할 로그 파일 (기본: 이어쓰기)')
    p.add_argument('--raw-log', metavar='PATH',
                   help='16진수 패킷만 한 줄씩 저장 (translate-packet.py 로 재해석 가능)')
    p.add_argument('--csv', metavar='PATH',
                   help='"ISO시각,16진수" 형식으로 저장 (analyze-log.py 프로토콜 분석용)')
    p.add_argument('--duration', metavar='SPEC',
                   help='지정 시간 후 자동 종료. 예: 3h, 90m, 45s, 3600 (기본: 무제한)')
    p.add_argument('--overwrite', action='store_true',
                   help='로그 파일을 이어쓰지 않고 새로 만든다')
    return p.parse_args()


def _fmt_duration(seconds: float) -> str:
    if seconds < 60:
        return f'{seconds:.0f}초'
    if seconds < 3600:
        return f'{seconds / 60:.1f}분'
    return f'{seconds / 3600:.2f}시간'


def _parse_duration(spec: str) -> float:
    """'3h' / '90m' / '45s' / '3600' → 초 단위 float."""
    text = spec.strip().lower()
    unit = {'h': 3600.0, 'm': 60.0, 's': 1.0}.get(text[-1:])
    value = text[:-1] if unit else text
    try:
        seconds = float(value) * (unit or 1.0)
    except ValueError:
        raise SystemExit(f'오류: --duration 값을 해석할 수 없습니다 → {spec!r} (예: 3h, 90m, 45s)')
    if seconds <= 0:
        raise SystemExit('오류: --duration 은 0보다 커야 합니다.')
    return seconds


async def main() -> None:
    global _LOG_FH, _RAW_FH, _CSV_FH

    args = _parse_args()
    duration = _parse_duration(args.duration) if args.duration else None

    # 한글·박스 문자가 깨지지 않도록 (Windows 콘솔 대비)
    try:
        sys.stdout.reconfigure(encoding='utf-8')  # type: ignore[union-attr]
    except Exception:
        pass

    mode = 'w' if args.overwrite else 'a'
    if args.log:
        _LOG_FH = open(args.log, mode, encoding='utf-8')
    if args.raw_log:
        _RAW_FH = open(args.raw_log, mode, encoding='utf-8')
    if args.csv:
        _CSV_FH = open(args.csv, mode, encoding='utf-8')

    translator = _load_translator() if args.decode != 'off' else None
    parser = PacketParser(decode=args.decode, translator=translator)

    started = datetime.now()
    out('=' * 60)
    out('  Kocom Wallpad RS485 패킷 로거')
    out(f'  시작 시각: {started.strftime("%Y-%m-%d %H:%M:%S")}')
    out(f'  패킷 형식: {_hex(PACKET_PREFIX)} ... {_hex(PACKET_SUFFIX)}  ({PACKET_LEN}B)')
    out(f'  해석 모드: {parser._decode}'
        + ('' if translator or args.decode == 'off' else '  (translate-packet.py 없음)'))
    if args.log:
        out(f'  로그 파일: {Path(args.log).resolve()}')
    if args.raw_log:
        out(f'  16진수 로그: {Path(args.raw_log).resolve()}')
    if args.csv:
        out(f'  분석용 CSV: {Path(args.csv).resolve()}')
    if duration:
        out(f'  캡처 시간: {_fmt_duration(duration)} 후 자동 종료')
    out('  종료: Ctrl+C')
    out('=' * 60)

    if args.type == 'socket':
        runner = run_socket(args.host, args.port, parser)
    else:
        runner = run_serial(args.serial_port, args.baud, parser)

    try:
        if duration is None:
            await runner
        else:
            try:
                await asyncio.wait_for(runner, timeout=duration)
            except asyncio.TimeoutError:
                out(f'[캡처] 지정 시간({args.duration}) 경과 — 종료합니다.')
    finally:
        elapsed = (datetime.now() - started).total_seconds()
        out('=' * 60)
        out(f'  종료 시각: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')
        out(f'  캡처 시간: {_fmt_duration(elapsed)}')
        out(f'  캡처 패킷: {parser._count:,}개'
            + (f'  ({parser._count / elapsed:.1f} pkt/s)' if elapsed > 0 else ''))
        out('=' * 60)
        for fh in (_LOG_FH, _RAW_FH, _CSV_FH):
            if fh is not None:
                fh.close()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print('\n종료합니다.')
