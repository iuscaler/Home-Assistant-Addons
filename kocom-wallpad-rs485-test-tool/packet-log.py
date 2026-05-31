#!/usr/bin/env python3
"""
Kocom Wallpad RS485 패킷 로거

RS485 시리얼 또는 TCP 소켓으로 수신되는 모든 패킷을 캡처하여 터미널에 출력한다.
패킷은 AA 55로 시작하고 0D 0D로 끝나는 21바이트 고정 길이다.

사용법:
  # 소켓 연결 (기본)
  python packet-log.py --type socket --host 192.168.1.100 --port 8899

  # 시리얼 연결
  python packet-log.py --type serial --serial-port /dev/ttyUSB0 --baud 9600
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime

# ── 패킷 상수 (kocom-wallpad-rs485/const.py와 동일) ─────────────────
PACKET_PREFIX = bytes([0xAA, 0x55])
PACKET_SUFFIX = bytes([0x0D, 0x0D])
PACKET_LEN    = 21


def _hex(data: bytes) -> str:
    return ' '.join(f'{b:02X}' for b in data)


class PacketParser:
    """스트림 바이트를 받아 완성된 패킷을 추출하고 출력한다."""

    def __init__(self) -> None:
        self._buf: bytearray = bytearray()
        self._count: int = 0

    def feed(self, data: bytes) -> None:
        self._buf.extend(data)
        self._extract()

    def _extract(self) -> None:
        while True:
            # AA 55 시작 위치 탐색
            idx = self._buf.find(PACKET_PREFIX)
            if idx == -1:
                if self._buf:
                    print(f'  [noise {len(self._buf)}B] {_hex(bytes(self._buf))}', flush=True)
                self._buf.clear()
                return

            # 시작 위치 이전 바이트는 노이즈로 처리
            if idx > 0:
                noise = bytes(self._buf[:idx])
                print(f'  [noise {len(noise)}B] {_hex(noise)}', flush=True)
                del self._buf[:idx]

            # 패킷 전체가 버퍼에 도착할 때까지 대기
            if len(self._buf) < PACKET_LEN:
                return

            packet = bytes(self._buf[:PACKET_LEN])

            # suffix 검증 — 맞지 않으면 prefix 1바이트를 버리고 재탐색
            if not packet.endswith(PACKET_SUFFIX):
                print(f'  [bad suffix] {_hex(packet)}', flush=True)
                del self._buf[0]
                continue

            del self._buf[:PACKET_LEN]
            self._print(packet)

    def _print(self, packet: bytes) -> None:
        self._count += 1
        ts = datetime.now().strftime('%H:%M:%S.%f')[:-3]
        print(f'[{ts}] #{self._count:04d}  {_hex(packet)}', flush=True)


# ── 연결별 수신 루프 ─────────────────────────────────────────────────

async def _recv_loop(reader: asyncio.StreamReader, parser: PacketParser) -> None:
    while True:
        chunk = await reader.read(512)
        if not chunk:
            raise ConnectionResetError('연결이 끊겼습니다.')
        parser.feed(chunk)


async def run_socket(host: str, port: int) -> None:
    parser = PacketParser()
    print(f'[소켓] {host}:{port} 연결 중...', flush=True)

    reconnect_delay = 5.0
    while True:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=10.0
            )
            print(f'[소켓] 연결됨 — 패킷 캡처 시작', flush=True)
            reconnect_delay = 5.0
            try:
                await _recv_loop(reader, parser)
            finally:
                writer.close()
        except asyncio.TimeoutError:
            print(f'[소켓] 연결 시간 초과. {reconnect_delay:.0f}초 후 재시도...', flush=True)
        except (ConnectionResetError, OSError) as e:
            print(f'[소켓] {e}. {reconnect_delay:.0f}초 후 재시도...', flush=True)

        await asyncio.sleep(reconnect_delay)
        reconnect_delay = min(reconnect_delay * 2, 60.0)
        print(f'[소켓] {host}:{port} 재연결 중...', flush=True)


async def run_serial(serial_port: str, baud: int) -> None:
    try:
        import serial_asyncio  # type: ignore
    except ImportError:
        print('오류: serial_asyncio 패키지가 없습니다. pip install pyserial-asyncio 실행 후 다시 시도하세요.')
        sys.exit(1)

    parser = PacketParser()
    print(f'[시리얼] {serial_port} @ {baud}bps 연결 중...', flush=True)

    reconnect_delay = 5.0
    while True:
        try:
            reader, writer = await serial_asyncio.open_serial_connection(
                url=serial_port, baudrate=baud
            )
            print(f'[시리얼] 연결됨 — 패킷 캡처 시작', flush=True)
            reconnect_delay = 5.0
            try:
                await _recv_loop(reader, parser)
            finally:
                writer.close()
        except (OSError, Exception) as e:
            print(f'[시리얼] {e}. {reconnect_delay:.0f}초 후 재시도...', flush=True)

        await asyncio.sleep(reconnect_delay)
        reconnect_delay = min(reconnect_delay * 2, 60.0)
        print(f'[시리얼] {serial_port} 재연결 중...', flush=True)


# ── 진입점 ──────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description='Kocom Wallpad RS485 패킷 로거',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            '예시:\n'
            '  python packet-log.py --type socket --host 192.168.1.100 --port 8899\n'
            '  python packet-log.py --type serial --serial-port /dev/ttyUSB0\n'
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
    return p.parse_args()


async def main() -> None:
    args = _parse_args()

    print('=' * 60)
    print('  Kocom Wallpad RS485 패킷 로거')
    print(f'  패킷 형식: {_hex(PACKET_PREFIX)} ... {_hex(PACKET_SUFFIX)}  ({PACKET_LEN}B)')
    print('  종료: Ctrl+C')
    print('=' * 60)

    if args.type == 'socket':
        await run_socket(args.host, args.port)
    else:
        await run_serial(args.serial_port, args.baud)


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print('\n종료합니다.')
