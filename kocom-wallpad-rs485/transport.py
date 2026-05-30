"""Async RS485 transport (serial or TCP socket)."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

import serial_asyncio  # type: ignore

log = logging.getLogger(__name__)


class AsyncRS485:
    """시리얼 포트 또는 TCP 소켓으로 RS485 버스에 연결하는 비동기 클래스."""

    def __init__(self, rs_type: str, serial_port: str, socket_host: str, socket_port: int) -> None:
        self._rs_type    = rs_type
        self._serial_port = serial_port
        self._socket_host = socket_host
        self._socket_port = socket_port

        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._last_rx: float = 0.0
        self._connected = False

    @classmethod
    def from_config(cls, config) -> 'AsyncRS485':
        rs_type     = config.get('RS485', 'type')
        serial_port = config.get('RS485', 'serial_port', fallback='/dev/ttyUSB0')
        host        = config.get('RS485', 'socket_server', fallback='')
        port        = int(config.get('RS485', 'socket_port', fallback='8899'))
        return cls(rs_type, serial_port, host, port)

    # ── 연결 관리 ────────────────────────────────────────────────
    async def open(self) -> None:
        try:
            if self._rs_type == 'serial':
                self._reader, self._writer = await serial_asyncio.open_serial_connection(
                    url=self._serial_port, baudrate=9600
                )
                log.info('[RS485] Serial connected: %s', self._serial_port)
            else:
                self._reader, self._writer = await asyncio.wait_for(
                    asyncio.open_connection(self._socket_host, self._socket_port),
                    timeout=10.0,
                )
                log.info('[RS485] Socket connected: %s:%s', self._socket_host, self._socket_port)
            self._connected = True
            self._touch()
        except Exception as e:
            log.error('[RS485] Connection failed: %r', e)
            self._connected = False

    async def close(self) -> None:
        if self._writer:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:
                pass
            self._writer = None
        self._reader = None
        self._connected = False

    async def reconnect(self) -> None:
        await self.close()
        delay = 5.0
        while True:
            log.info('[RS485] Reconnecting in %.0fs...', delay)
            await asyncio.sleep(delay)
            await self.open()
            if self._connected:
                log.info('[RS485] Reconnected.')
                return
            delay = min(delay * 2, 60.0)

    # ── 상태 조회 ────────────────────────────────────────────────
    def is_connected(self) -> bool:
        return self._connected

    def idle_since(self) -> float:
        """마지막 수신 이후 경과 시간 (초)."""
        return max(0.0, time.monotonic() - self._last_rx)

    def _touch(self) -> None:
        self._last_rx = time.monotonic()

    # ── 송수신 ──────────────────────────────────────────────────
    async def send(self, data: bytes) -> bool:
        if not self._writer:
            return False
        try:
            self._writer.write(data)
            await self._writer.drain()
            log.debug('[RS485] TX: %s', data.hex())
            return True
        except Exception as e:
            log.warning('[RS485] Send failed: %r', e)
            self._connected = False
            return False

    async def recv(self, nbytes: int = 512, timeout: float = 0.05) -> bytes:
        if not self._reader:
            return b''
        try:
            chunk = await asyncio.wait_for(self._reader.read(nbytes), timeout=timeout)
            if chunk:
                self._touch()
            return chunk
        except asyncio.TimeoutError:
            return b''
        except Exception as e:
            log.warning('[RS485] Recv failed: %r', e)
            self._connected = False
            return b''
