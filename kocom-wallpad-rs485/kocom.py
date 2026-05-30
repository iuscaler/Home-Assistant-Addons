#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Kocom Wallpad RS485 ↔ MQTT bridge — entrypoint.

KocomBridge: RS485 수신 루프 / 송신 큐 / MQTT 연동 / 폴링 오케스트레이터
main():       MQTT 연결 + 자동 재연결 루프
"""

import asyncio
import json
import logging

import aiomqtt  # type: ignore

from const import (
    SW_VERSION,
    IDLE_GAP, SEND_RETRY, SEND_RETRY_GAP, POLLING_INTERVAL,
    CODE_DEVICE, NO_POLL_DEVICES,
)
from controller import KocomController
from discovery import publish_discovery
from options import Options
from transport import AsyncRS485

logging.basicConfig(
    format='%(levelname)s[%(asctime)s]: %(message)s',
    level=logging.INFO,
)
log = logging.getLogger(__name__)


class KocomBridge:
    """
    RS485 ↔ MQTT 브리지 오케스트레이터.

    - _read_loop:   RS485 수신 → KocomController.feed()
    - _sender_loop: TX 큐에서 패킷을 꺼내 RS485로 송신
    - _poll_loop:   주기적으로 장치 상태 조회
    - handle_command: MQTT command 토픽을 파싱하여 패킷을 TX 큐에 적재
    """

    def __init__(self, config: Options, mqtt: aiomqtt.Client) -> None:
        self._config   = config
        self._mqtt     = mqtt
        self._rs485    = AsyncRS485.from_config(config)
        self._tx_queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._ctrl     = KocomController(on_state=self._publish, config=config)

    # ── 공개 진입점 ──────────────────────────────────────────────
    async def run(self) -> None:
        await self._rs485.open()
        try:
            await asyncio.gather(
                self._read_loop(),
                self._sender_loop(),
                self._poll_loop(),
            )
        finally:
            await self._rs485.close()

    # ── 수신 루프 ────────────────────────────────────────────────
    async def _read_loop(self) -> None:
        log.info('[Bridge] Read loop started.')
        while True:
            if not self._rs485.is_connected():
                await self._rs485.reconnect()
            chunk = await self._rs485.recv()
            if chunk:
                if self._config.get('Log', 'show_recv_hex', fallback='False') == 'True':
                    log.info('[RS485] RX raw: %s', chunk.hex())
                self._ctrl.feed(chunk)

    # ── 송신 루프 ────────────────────────────────────────────────
    async def _sender_loop(self) -> None:
        log.info('[Bridge] Sender loop started.')
        while True:
            packet = await self._tx_queue.get()
            if not self._rs485.is_connected():
                log.warning('[TX] Not connected, dropping packet.')
                self._tx_queue.task_done()
                continue

            for attempt in range(1, SEND_RETRY + 1):
                # 버스 유휴 대기
                t0 = asyncio.get_running_loop().time()
                while self._rs485.idle_since() < IDLE_GAP:
                    await asyncio.sleep(0.005)
                    if asyncio.get_running_loop().time() - t0 > 1.0:
                        break

                ok = await self._rs485.send(packet)
                if ok:
                    break
                if attempt < SEND_RETRY:
                    await asyncio.sleep(SEND_RETRY_GAP)

            self._tx_queue.task_done()

    # ── 폴링 루프 ────────────────────────────────────────────────
    async def _poll_loop(self) -> None:
        log.info('[Bridge] Poll loop started.')
        await asyncio.sleep(3)
        while True:
            await self._poll_once()
            await asyncio.sleep(POLLING_INTERVAL)

    async def _poll_once(self) -> None:
        for entry in self._config.get_devices():
            dev = entry.get('type', '')
            if dev in NO_POLL_DEVICES or dev not in CODE_DEVICE:
                continue
            room = entry.get('room', 'livingroom')
            await self._tx_queue.put(self._ctrl.build_query(dev, room))
            await asyncio.sleep(0.5)

    # ── MQTT 발행 콜백 (controller → MQTT) ──────────────────────
    async def _publish(self, topic: str, payload: dict) -> None:
        try:
            await self._mqtt.publish(topic, json.dumps(payload), qos=0, retain=True)
        except Exception as e:
            log.warning('[MQTT] Publish failed %s: %r', topic, e)
        if self._config.get('Log', 'show_mqtt_publish', fallback='False') == 'True':
            log.info('[MQTT] %s → %s', topic, payload)

    # ── MQTT 커맨드 수신 (MQTT → RS485) ─────────────────────────
    async def handle_command(self, topic: str, payload: str) -> None:
        """MQTT command 토픽을 파싱하여 RS485 패킷을 TX 큐에 적재."""
        parts = topic.split('/')
        if parts[-1] != 'command':
            return

        cmd = payload.strip()
        log.info('[CMD] %s → %s', topic, cmd)

        try:
            packets = self._route(parts, cmd)
        except Exception as e:
            log.warning('[CMD] Route error %s: %r', topic, e)
            return

        for pkt in packets:
            await self._tx_queue.put(pkt)

    def _route(self, parts: list[str], cmd: str) -> list[bytes]:
        """토픽 parts → controller.build_command 호출."""
        # kocom/room/thermo/{idx}/heat_mode/command
        if 'thermo' in parts and 'heat_mode' in parts:
            room_idx = parts[3]
            return self._ctrl.build_command('thermo', room_idx, 'heat_mode', heat_mode=cmd)

        # kocom/room/thermo/{idx}/set_temp/command
        if 'thermo' in parts and 'set_temp' in parts:
            room_idx = parts[3]
            return self._ctrl.build_command('thermo', room_idx, 'set_temp', set_temp=cmd)

        # kocom/{room}/aircon/hvac/command
        if 'aircon' in parts and 'hvac' in parts:
            return self._ctrl.build_command('aircon', parts[1], 'hvac', hvac=cmd)

        # kocom/{room}/aircon/fan/command
        if 'aircon' in parts and 'fan' in parts:
            return self._ctrl.build_command('aircon', parts[1], 'fan', fan=cmd)

        # kocom/{room}/aircon/temp/command
        if 'aircon' in parts and 'temp' in parts:
            return self._ctrl.build_command('aircon', parts[1], 'temp', temp=cmd)

        # kocom/{room}/light/command (단일) 또는 kocom/{room}/light/{n}/command (복수)
        if 'light' in parts:
            try:
                index = int(parts[3])
            except (ValueError, IndexError):
                index = 1
            return self._ctrl.build_command('light', parts[1], cmd, index=index)

        # kocom/{room}/outlet/command (단일) 또는 kocom/{room}/outlet/{n}/command (복수)
        if 'outlet' in parts:
            try:
                index = int(parts[3])
            except (ValueError, IndexError):
                index = 1
            return self._ctrl.build_command('outlet', parts[1], cmd, index=index)

        # kocom/{room}/gas/command
        if 'gas' in parts:
            if cmd != 'off':
                log.info('[CMD] Gas: only off is allowed.')
                return []
            return self._ctrl.build_command('gas', parts[1], cmd)

        # kocom/myhome/elevator/command
        if 'elevator' in parts:
            if cmd != 'on':
                return []
            elev_type = self._config.get('Elevator', 'type', fallback='rs485')
            if elev_type == 'tcpip':
                asyncio.get_running_loop().create_task(self._call_elevator_tcpip())
                return []
            return self._ctrl.build_command('elevator', 'myhome', cmd)

        # kocom/{room}/fan/set_speed/command
        if 'fan' in parts and 'set_speed' in parts:
            return self._ctrl.build_command('fan', parts[1], 'speed', speed=cmd)

        # kocom/{room}/fan/set_timer/command
        if 'fan' in parts and 'set_timer' in parts:
            return self._ctrl.build_command('fan', parts[1], 'timer', hours=cmd)

        # kocom/{room}/fan/set_preset_mode/command
        if 'fan' in parts and 'set_preset_mode' in parts:
            return self._ctrl.build_command('fan', parts[1], 'preset', preset=cmd)

        # kocom/{room}/fan/command
        if 'fan' in parts:
            return self._ctrl.build_command('fan', parts[1], cmd)

        # kocom/myhome/query/command
        if 'query' in parts and cmd == 'PRESS':
            asyncio.get_running_loop().create_task(self._poll_once())

        return []

    # ── TCP/IP 엘리베이터 호출 ───────────────────────────────────
    async def _call_elevator_tcpip(self) -> None:
        server  = self._config.get('Elevator', 'tcpip_apt_server')
        port    = int(self._config.get('Elevator', 'tcpip_apt_port'))
        p1      = bytes.fromhex(self._config.get('Elevator', 'tcpip_packet1'))
        p2      = bytes.fromhex(self._config.get('Elevator', 'tcpip_packet2'))
        p3      = bytes.fromhex(self._config.get('Elevator', 'tcpip_packet3'))
        p4_hex  = self._config.get('Elevator', 'tcpip_packet4')
        try:
            r, w = await asyncio.wait_for(asyncio.open_connection(server, port), timeout=10.0)
            w.write(p1); await w.drain(); await r.read(512)
            await asyncio.sleep(0.1)
            w.write(p2); await w.drain(); await r.read(512)
            w.write(p3); await w.drain()
            for _ in range(100):
                rcv = await r.read(512)
                if not rcv or rcv.hex() == p4_hex:
                    break
            w.write(p2); await w.drain(); await r.read(512)
            w.close(); await w.wait_closed()
            log.info('[Elevator] TCPIP call done.')
        except Exception as e:
            log.error('[Elevator] TCPIP failed: %r', e)


# ── 엔트리포인트 ──────────────────────────────────────────────────
async def main() -> None:
    config = Options()

    if config.get('Log', 'show_recv_hex', fallback='False') == 'True':
        logging.getLogger().setLevel(logging.DEBUG)

    log.info('[Main] Kocom Wallpad RS485 bridge v%s starting...', SW_VERSION)

    mqtt_server = config.get('MQTT', 'mqtt_server')
    mqtt_port   = int(config.get('MQTT', 'mqtt_port'))
    anon        = config.get('MQTT', 'mqtt_allow_anonymous') == 'True'
    username    = None if anon else (config.get('MQTT', 'mqtt_username', fallback='') or None)
    password    = None if anon else (config.get('MQTT', 'mqtt_password', fallback='') or None)

    reconnect_interval = 5
    while True:
        try:
            async with aiomqtt.Client(
                hostname=mqtt_server,
                port=mqtt_port,
                username=username,
                password=password,
            ) as client:
                bridge = KocomBridge(config, client)
                await client.subscribe('kocom/#', qos=0)
                await publish_discovery(client, config)

                async def mqtt_listen() -> None:
                    async for msg in client.messages:
                        topic   = str(msg.topic)
                        payload = msg.payload.decode('utf-8', errors='replace')
                        await bridge.handle_command(topic, payload)

                await asyncio.gather(
                    bridge.run(),
                    mqtt_listen(),
                )
        except aiomqtt.MqttError as e:
            log.warning('[MQTT] Disconnected (%s). Reconnecting in %ds...', e, reconnect_interval)
            await asyncio.sleep(reconnect_interval)
        except Exception as e:
            log.exception('[Main] Unexpected error: %r', e)
            await asyncio.sleep(reconnect_interval)


if __name__ == '__main__':
    asyncio.run(main())
