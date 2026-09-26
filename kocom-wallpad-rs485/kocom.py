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
    DIAG_INTERVAL, BUS_ALIVE_TIMEOUT, TIME_DRIFT_WARN, AWAY_STEP_GAP,
)

AVAILABILITY_TOPIC = 'kocom/bridge/availability'
DIAG_TOPIC         = 'kocom/bridge/diagnostics'
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
        self._last_drift: float = 0.0

    # ── 공개 진입점 ──────────────────────────────────────────────
    async def run(self) -> None:
        await self._rs485.open()
        try:
            await asyncio.gather(
                self._read_loop(),
                self._sender_loop(),
                self._poll_loop(),
                self._diag_loop(),
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
                    self._ctrl.diag['tx_total'] += 1
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

    # ── 진단 루프 ────────────────────────────────────────────────
    async def _diag_loop(self) -> None:
        """월패드·EW11이 살아 있는지 주기적으로 발행.

        버스는 유휴 시 5분 넘게 조용할 수 있으므로(실측 최대 4.8분) 단순히
        '최근 패킷 없음'만으로 장애를 판정하지 않는다. 대신 마지막 수신 경과
        시간을 그대로 노출하고, BUS_ALIVE_TIMEOUT을 넘으면 끊김으로 본다.

        월패드가 스스로 보내는 시각 프레임은 버스가 정상임을 보여주는 가장
        좋은 신호라, 시각과 오차를 함께 발행한다.
        """
        log.info('[Bridge] Diagnostics loop started.')
        while True:
            d   = self._ctrl.diagnostics()
            age = d['last_rx_age']
            d['bus'] = 'off' if age is None or age > BUS_ALIVE_TIMEOUT else 'on'
            d['rs485'] = 'on' if self._rs485.is_connected() else 'off'

            d['time_sync'] = (
                'unknown' if d['wallpad_time'] is None
                else 'ok' if abs(self._last_drift) <= TIME_DRIFT_WARN
                else 'drift'
            )
            await self._publish(DIAG_TOPIC, d)
            await asyncio.sleep(DIAG_INTERVAL)

    # ── 외출모드 매크로 ──────────────────────────────────────────
    async def _run_away_macro(self, on: bool) -> None:
        """외출모드는 여러 장치에 연쇄 명령을 보내므로 간격을 두고 전송한다.

        월패드는 단계 사이에 약 2초를 둔다. 큐에 한꺼번에 넣으면 수십 ms
        간격으로 쏟아져 장치가 따라오지 못할 수 있어 같은 간격을 모사한다.
        """
        for i, pkt in enumerate(self._ctrl.build_away(on)):
            if i:
                await asyncio.sleep(AWAY_STEP_GAP)
            await self._tx_queue.put(pkt)
        log.info('[Away] Macro %s done (%s packets).', 'on' if on else 'off', i + 1)

    # ── MQTT 발행 콜백 (controller → MQTT) ──────────────────────
    async def _publish(self, topic: str, payload: dict) -> None:
        if topic.endswith('/wallpad/time'):
            self._last_drift = float(payload.get('drift', 0.0))
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

        # kocom/room/thermo/{idx}/away/command — 외출 모드
        if 'thermo' in parts and 'away' in parts:
            return self._ctrl.build_command('thermo', parts[3], 'away', away=cmd)

        # kocom/myhome/lightcutoff/command — 전체 소등 / 해제
        if 'lightcutoff' in parts:
            return self._ctrl.build_command('lightcutoff', 'myhome', cmd)

        # kocom/myhome/away/command — 외출모드 매크로 (간격을 두고 전송)
        if 'away' in parts:
            asyncio.get_running_loop().create_task(self._run_away_macro(cmd == 'on'))
            return []

        # kocom/{room}/light/command (단일) / {n}/command (복수) / all/command (방 전체)
        if 'light' in parts:
            index: int | str = 1
            try:
                index = 'all' if parts[3] == 'all' else int(parts[3])
            except (ValueError, IndexError):
                index = 1
            return self._ctrl.build_command('light', parts[1], cmd, index=index)

        # kocom/{room}/outlet/command (단일) / {n}/command (복수) / all/command (방 전체)
        if 'outlet' in parts:
            index = 1
            try:
                index = 'all' if parts[3] == 'all' else int(parts[3])
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


# ── MQTT 클라이언트 생성 ──────────────────────────────────────────
def _make_client(mqtt_cfg: dict) -> aiomqtt.Client:
    """LWT(Last Will)를 설정한 MQTT 클라이언트.

    브리지가 죽으면 브로커가 대신 offline을 발행하므로 HA에서 모든 엔티티가
    unavailable로 바뀐다. aiomqtt는 버전이 고정돼 있지 않으므로, Will API가
    다른 버전에서도 애드온이 기동하도록 실패 시 LWT 없이 연결한다.
    """
    kwargs = dict(
        hostname=mqtt_cfg['server'],
        port=mqtt_cfg['port'],
        username=mqtt_cfg['username'],
        password=mqtt_cfg['password'],
    )
    try:
        return aiomqtt.Client(
            **kwargs,
            will=aiomqtt.Will(AVAILABILITY_TOPIC, b'offline', qos=1, retain=True),
        )
    except (AttributeError, TypeError) as e:
        log.warning('[MQTT] LWT 설정 실패 (%r). 가용성 알림 없이 연결합니다.', e)
        return aiomqtt.Client(**kwargs)


# ── 엔트리포인트 ──────────────────────────────────────────────────
async def main() -> None:
    config = Options()

    if config.get('Log', 'show_recv_hex', fallback='False') == 'True':
        logging.getLogger().setLevel(logging.DEBUG)

    log.info('[Main] Kocom Wallpad RS485 bridge v%s starting...', SW_VERSION)

    # MQTT 접속 정보 결정: 수동 설정 → Supervisor 자동 발견 → 기본값 폴백
    # (자세한 우선순위는 Options.get_mqtt 참고)
    mqtt_cfg = config.get_mqtt()
    log.info(
        '[MQTT] Broker %s:%s (user=%s, source=%s)',
        mqtt_cfg['server'], mqtt_cfg['port'],
        mqtt_cfg['username'] or '(anonymous)', mqtt_cfg['source'],
    )
    if mqtt_cfg['ssl']:
        # 브로커가 SSL 포트를 서비스로 알려온 경우 — TLS 클라이언트 미구현 상태
        log.warning('[MQTT] 브로커가 SSL을 요구하지만 아직 지원하지 않습니다. 평문으로 시도합니다.')

    reconnect_interval = 5
    while True:
        try:
            async with _make_client(mqtt_cfg) as client:
                bridge = KocomBridge(config, client)
                await client.subscribe('kocom/#', qos=0)
                await client.publish(AVAILABILITY_TOPIC, b'online', qos=1, retain=True)
                await publish_discovery(client, config)
                # 브로커가 재시작해 retain 메시지를 잃었을 수 있으므로,
                # 중복 억제 캐시를 비워 다음 상태 프레임부터 다시 발행한다.
                bridge._ctrl.invalidate_pub_cache()

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
