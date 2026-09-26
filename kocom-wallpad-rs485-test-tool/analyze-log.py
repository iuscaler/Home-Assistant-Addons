#!/usr/bin/env python3
"""
Kocom Wallpad RS485 패킷 로그 프로토콜 분석기

packet-log.py 로 장시간 캡처한 로그를 읽어 버스 위에서 오가는 대화 구조를
통계로 정리한다. 누가 누구에게 어떤 주기로 무엇을 보내는지, 요청에 대한
응답 지연은 얼마인지, 상태가 언제 바뀌었는지를 뽑아낸다.

입력 형식은 자동 판별한다.
  1) packet-log.py --csv      "2026-08-09T14:23:01.452,AA5530BC..."  (권장, 시각 포함)
  2) packet-log.py --log      "[14:23:01.452] #0001  AA 55 30 BC ..."
  3) packet-log.py --raw-log  "AA 55 30 BC ..."                      (시각 없음)

사용법:
  python analyze-log.py capture.csv
  python analyze-log.py capture.csv --out report.txt --top 25
"""

from __future__ import annotations

import argparse
import importlib.util
import re
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, NamedTuple

PACKET_LEN = 21

# 이 시간 안에 돌아온 반대 방향 패킷을 응답으로 간주한다.
RESPONSE_WINDOW_SEC = 2.0
# 간격의 변동계수(stdev/mean)가 이 값 이하이면 "주기적"으로 판정한다.
PERIODIC_CV = 0.25
# 주기 판정에 필요한 최소 샘플 수
PERIODIC_MIN_SAMPLES = 5


# ── translate-packet.py 재사용 ───────────────────────────────────────
def _load_translator() -> Any | None:
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
    except Exception as e:
        print(f'경고: translate-packet.py 로드 실패 ({e}) — 코드 이름 없이 분석합니다.')
        return None


TR = _load_translator()


def dev_label(code: int) -> str:
    if TR is not None:
        name = TR.DEVICE_NAME.get(code)
        return f'{name}(0x{code:02X})' if name else f'미상(0x{code:02X})'
    return f'0x{code:02X}'


def room_label(code: int) -> str:
    if TR is not None:
        name = TR.ROOM_NAME.get(code)
        return name if name else f'방0x{code:02X}'
    return f'0x{code:02X}'


def cmd_label(code: int) -> str:
    name = TR.COMMAND_NAME.get(code, '') if TR is not None else ''
    return f'0x{code:02X}' + (f'({name})' if name else '')


# ── 패킷 표현 ────────────────────────────────────────────────────────
class Pkt(NamedTuple):
    ts: datetime | None
    raw: bytes

    @property
    def ptype(self) -> int:
        return (self.raw[3] >> 4) & 0x0F

    @property
    def dst_dev(self) -> int:
        return self.raw[5]

    @property
    def dst_room(self) -> int:
        return self.raw[6]

    @property
    def src_dev(self) -> int:
        return self.raw[7]

    @property
    def src_room(self) -> int:
        return self.raw[8]

    @property
    def cmd(self) -> int:
        return self.raw[9]

    @property
    def payload(self) -> bytes:
        return self.raw[10:18]

    @property
    def checksum_ok(self) -> bool:
        return sum(self.raw[2:18]) % 256 == self.raw[18]

    @property
    def from_wallpad(self) -> bool:
        return self.src_dev == 0x01

    @property
    def peer(self) -> tuple[int, int]:
        """월패드가 아닌 쪽 (장치코드, 방코드)."""
        if self.dst_dev == 0x01:
            return self.src_dev, self.src_room
        return self.dst_dev, self.dst_room


# ── 입력 파싱 ────────────────────────────────────────────────────────
_CSV_RE = re.compile(r'^(?P<ts>\d{4}-\d{2}-\d{2}T[\d:.]+)\s*,\s*(?P<hex>[0-9A-Fa-f]+)\s*$')
_LOG_RE = re.compile(r'^\[(?P<ts>\d{2}:\d{2}:\d{2}\.\d+)\]\s+#\d+\s+(?P<hex>[0-9A-Fa-f ]+?)\s*$')
_HEX_RE = re.compile(r'^[0-9A-Fa-f\s\-:]+$')


def load_packets(path: Path) -> tuple[list[Pkt], dict[str, int]]:
    """로그 파일에서 패킷 목록과 파싱 통계를 뽑는다."""
    packets: list[Pkt] = []
    stats = {'lines': 0, 'skipped': 0, 'bad_len': 0, 'bad_checksum': 0, 'bad_frame': 0}

    # --log 형식은 날짜가 없다. 시각이 되감기면 자정을 넘긴 것으로 보고 하루 더한다.
    day_offset = timedelta(0)
    prev_clock: datetime | None = None
    base_date = datetime.fromtimestamp(path.stat().st_mtime).replace(
        hour=0, minute=0, second=0, microsecond=0
    )

    with path.open(encoding='utf-8', errors='replace') as fh:
        for line in fh:
            stats['lines'] += 1
            line = line.strip()
            if not line or line.startswith('#'):
                continue

            ts: datetime | None = None
            hex_text: str | None = None

            m = _CSV_RE.match(line)
            if m:
                ts = datetime.fromisoformat(m.group('ts'))
                hex_text = m.group('hex')
            else:
                m = _LOG_RE.match(line)
                if m:
                    clock = datetime.strptime(m.group('ts'), '%H:%M:%S.%f')
                    ts = base_date.replace(
                        hour=clock.hour, minute=clock.minute,
                        second=clock.second, microsecond=clock.microsecond,
                    )
                    if prev_clock is not None and ts < prev_clock:
                        day_offset += timedelta(days=1)
                    prev_clock = ts
                    ts += day_offset
                    hex_text = m.group('hex')
                elif _HEX_RE.match(line):
                    hex_text = line

            if hex_text is None:
                stats['skipped'] += 1
                continue

            clean = re.sub(r'[\s\-:]', '', hex_text)
            if len(clean) % 2 != 0:
                stats['skipped'] += 1
                continue

            raw = bytes.fromhex(clean)
            if len(raw) != PACKET_LEN:
                stats['bad_len'] += 1
                continue

            pkt = Pkt(ts, raw)
            if raw[:2] != b'\xaa\x55' or raw[-2:] != b'\x0d\x0d':
                stats['bad_frame'] += 1
                continue
            if not pkt.checksum_ok:
                stats['bad_checksum'] += 1

            packets.append(pkt)

    return packets, stats


# ── 통계 헬퍼 ────────────────────────────────────────────────────────
def _fmt_sec(v: float) -> str:
    if v < 1:
        return f'{v * 1000:.0f}ms'
    if v < 60:
        return f'{v:.2f}s'
    if v < 3600:
        return f'{v / 60:.1f}분'
    return f'{v / 3600:.2f}시간'


def _interval_stats(times: list[datetime]) -> dict[str, float] | None:
    if len(times) < 2:
        return None
    diffs = [(b - a).total_seconds() for a, b in zip(times, times[1:])]
    mean = statistics.fmean(diffs)
    stdev = statistics.stdev(diffs) if len(diffs) > 1 else 0.0
    return {
        'n': len(diffs),
        'mean': mean,
        'median': statistics.median(diffs),
        'min': min(diffs),
        'max': max(diffs),
        'stdev': stdev,
        'cv': (stdev / mean) if mean > 0 else 0.0,
    }


def _detect_cycle(symbols: list[str], max_period: int = 60) -> tuple[int, float] | None:
    """심볼 시퀀스에서 반복 주기를 찾는다. (주기, 일치율) 또는 None."""
    n = len(symbols)
    if n < 20:
        return None
    best: tuple[int, float] | None = None
    for period in range(1, min(max_period, n // 3) + 1):
        matches = sum(1 for i in range(n - period) if symbols[i] == symbols[i + period])
        ratio = matches / (n - period)
        if ratio > 0.9 and (best is None or ratio > best[1]):
            best = (period, ratio)
            if ratio > 0.99:
                break
    return best


class Report:
    """리포트 줄을 모아 화면과 파일에 함께 출력한다."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def __call__(self, text: str = '') -> None:
        self.lines.append(text)

    def head(self, title: str) -> None:
        self('')
        self('━' * 78)
        self(f'  {title}')
        self('━' * 78)

    def text(self) -> str:
        return '\n'.join(self.lines)


# ── 분석 섹션 ────────────────────────────────────────────────────────
def section_overview(r: Report, packets: list[Pkt], stats: dict[str, int], path: Path) -> float:
    r.head('1. 캡처 개요')
    r(f'  로그 파일   : {path}')
    r(f'  읽은 줄     : {stats["lines"]:,}')
    r(f'  유효 패킷   : {len(packets):,}')
    if stats['bad_len']:
        r(f'  길이 오류   : {stats["bad_len"]:,}')
    if stats['bad_frame']:
        r(f'  프레임 오류 : {stats["bad_frame"]:,}  (AA55/0D0D 불일치)')
    if stats['bad_checksum']:
        pct = stats['bad_checksum'] / max(len(packets), 1) * 100
        r(f'  체크섬 오류 : {stats["bad_checksum"]:,}  ({pct:.2f}%)  ← 노이즈/충돌 의심')
    if stats['skipped']:
        r(f'  무시한 줄   : {stats["skipped"]:,}')

    timed = [p for p in packets if p.ts is not None]
    if len(timed) < 2:
        r('  ⚠ 시각 정보가 없어 주기 분석은 생략됩니다. --csv 로그를 사용하세요.')
        return 0.0

    start, end = timed[0].ts, timed[-1].ts
    elapsed = (end - start).total_seconds()
    r(f'  캡처 구간   : {start:%Y-%m-%d %H:%M:%S} ~ {end:%Y-%m-%d %H:%M:%S}')
    r(f'  캡처 시간   : {_fmt_sec(elapsed)}')
    if elapsed > 0:
        r(f'  평균 트래픽 : {len(timed) / elapsed:.2f} pkt/s  ({len(timed) / elapsed * 60:.0f} pkt/분)')
    return elapsed


def section_traffic(r: Report, packets: list[Pkt]) -> None:
    r.head('2. 트래픽 구성')

    ptypes = Counter(p.ptype for p in packets)
    r('  [패킷 타입]')
    for code, cnt in ptypes.most_common():
        name = TR.PACKET_TYPE_NAME.get(code, '미상') if TR else '?'
        r(f'    0x{code:02X} {name:<10} {cnt:>8,}  ({cnt / len(packets) * 100:5.1f}%)')

    r('')
    r('  [장치별 트래픽]  peer = 월패드 반대편 장치')
    by_peer: Counter[tuple[int, int]] = Counter()
    tx: Counter[tuple[int, int]] = Counter()
    rx: Counter[tuple[int, int]] = Counter()
    for p in packets:
        key = p.peer
        by_peer[key] += 1
        if p.from_wallpad:
            rx[key] += 1   # 월패드 → 장치
        else:
            tx[key] += 1   # 장치 → 월패드
    r(f'    {"장치":<22} {"방":<8} {"총계":>8} {"월패드→":>8} {"→월패드":>8}')
    for (dev, room), cnt in by_peer.most_common():
        r(f'    {dev_label(dev):<22} {room_label(room):<8} {cnt:>8,} {rx[(dev, room)]:>8,} {tx[(dev, room)]:>8,}')

    unknown_dev = {p.peer[0] for p in packets if TR and p.peer[0] not in TR.DEVICE_NAME}
    unknown_cmd = {p.cmd for p in packets if TR and p.cmd not in TR.COMMAND_NAME}
    if unknown_dev or unknown_cmd:
        r('')
        r('  [미등록 코드]  translate-packet.py 매핑에 없는 값')
        if unknown_dev:
            r(f'    장치 코드 : {", ".join(f"0x{c:02X}" for c in sorted(unknown_dev))}')
        if unknown_cmd:
            r(f'    커맨드    : {", ".join(f"0x{c:02X}" for c in sorted(unknown_cmd))}')


def _flow_key(p: Pkt) -> tuple:
    return (p.src_dev, p.src_room, p.dst_dev, p.dst_room, p.cmd, p.ptype)


def _flow_name(key: tuple) -> str:
    sd, sr, dd, dr, cmd, ptype = key
    src = '월패드' if sd == 0x01 else f'{dev_label(sd)} {room_label(sr)}'
    dst = '월패드' if dd == 0x01 else f'{dev_label(dd)} {room_label(dr)}'
    kind = 'ACK' if ptype == 0x0D else '전송'
    return f'{src} → {dst}  {cmd_label(cmd)} [{kind}]'


def section_flows(r: Report, packets: list[Pkt], top: int) -> None:
    r.head('3. 대화(flow)별 주기  —  발신·수신·커맨드 조합 기준')

    groups: dict[tuple, list[Pkt]] = defaultdict(list)
    for p in packets:
        groups[_flow_key(p)].append(p)

    rows = []
    for key, pkts in groups.items():
        times = [p.ts for p in pkts if p.ts is not None]
        rows.append((len(pkts), key, _interval_stats(times)))
    rows.sort(key=lambda x: -x[0])

    r(f'  {"대화":<44} {"횟수":>7} {"중앙간격":>10} {"편차":>9} {"판정":<10}')
    r(f'  {"-" * 44} {"-" * 7} {"-" * 10} {"-" * 9} {"-" * 10}')
    for cnt, key, st in rows[:top]:
        if st is None:
            r(f'  {_flow_name(key)[:44]:<44} {cnt:>7,} {"-":>10} {"-":>9} {"단발":<10}')
            continue
        periodic = (st['cv'] <= PERIODIC_CV and st['n'] >= PERIODIC_MIN_SAMPLES)
        verdict = '주기적' if periodic else ('불규칙' if st['n'] >= PERIODIC_MIN_SAMPLES else '샘플부족')
        r(f'  {_flow_name(key)[:44]:<44} {cnt:>7,} {_fmt_sec(st["median"]):>10} '
          f'{"±" + _fmt_sec(st["stdev"]):>9} {verdict:<10}')
    if len(rows) > top:
        r(f'  ... 외 {len(rows) - top}개 (--top 옵션으로 더 보기)')

    periodic_rows = [
        (cnt, key, st) for cnt, key, st in rows
        if st and st['cv'] <= PERIODIC_CV and st['n'] >= PERIODIC_MIN_SAMPLES
    ]
    if periodic_rows:
        r('')
        r('  [주기적 대화 상세]  min ~ max 는 관측된 간격의 범위')
        for cnt, key, st in periodic_rows[:top]:
            r(f'    {_flow_name(key)}')
            r(f'      주기 {_fmt_sec(st["median"])} (평균 {_fmt_sec(st["mean"])}, '
              f'범위 {_fmt_sec(st["min"])} ~ {_fmt_sec(st["max"])}, CV {st["cv"]:.2f}, n={cnt:,})')


def section_polling_cycle(r: Report, packets: list[Pkt]) -> None:
    r.head('4. 월패드 폴링 순환 구조')

    # ACK(0x0D)는 순서 구조와 무관하므로 실제 전송(0x0B)만 본다
    requests = [p for p in packets if p.from_wallpad and p.ptype == 0x0B]
    if len(requests) < 20:
        r(f'  월패드가 먼저 보낸 전송(0x0B) 패킷이 {len(requests)}개뿐이라 '
          '순환 구조를 판정할 수 없습니다.')
        r('  → 이 버스에서는 월패드가 폴링을 주도하는 프레임이 관측되지 않습니다.')
        return

    symbols = [f'{p.dst_dev:02X}{p.dst_room:02X}{p.cmd:02X}' for p in requests]
    cycle = _detect_cycle(symbols)
    if cycle is None:
        r('  뚜렷한 반복 순서를 찾지 못했습니다 (이벤트 구동형이거나 폴링 순서가 가변적입니다).')
    else:
        period, ratio = cycle
        r(f'  반복 주기 : 요청 {period}개마다 같은 순서가 반복됩니다 (일치율 {ratio * 100:.1f}%)')
        r('')
        r('  [한 사이클 순서]')
        for i, p in enumerate(requests[:period], 1):
            target = '월패드' if p.dst_dev == 0x01 else f'{dev_label(p.dst_dev)} {room_label(p.dst_room)}'
            r(f'    {i:2d}. {target:<28} {cmd_label(p.cmd)}')

        starts = [p.ts for i, p in enumerate(requests) if i % period == 0 and p.ts]
        st = _interval_stats(starts)
        if st:
            r('')
            r(f'  사이클 한 바퀴 : 중앙값 {_fmt_sec(st["median"])} '
              f'(범위 {_fmt_sec(st["min"])} ~ {_fmt_sec(st["max"])}, n={st["n"]:,})')

    st_all = _interval_stats([p.ts for p in requests if p.ts])
    if st_all:
        r(f'  요청 간 간격   : 중앙값 {_fmt_sec(st_all["median"])} '
          f'(평균 {_fmt_sec(st_all["mean"])}, 최소 {_fmt_sec(st_all["min"])})')


def section_response(r: Report, packets: list[Pkt], top: int) -> None:
    r.head('5. 전송(0x0B) → ACK(0x0D) 확인')

    timed = [p for p in packets if p.ts is not None]
    if len(timed) < 2:
        r('  시각 정보가 없어 생략합니다.')
        return

    # ACK = 바로 뒤에 오는, 주소를 뒤집고 커맨드·페이로드가 같은 0x0D 패킷
    latencies: dict[tuple[int, int], list[float]] = defaultdict(list)
    missing: Counter[tuple[int, int]] = Counter()
    total: Counter[tuple[int, int]] = Counter()

    for i, p in enumerate(timed):
        if p.ptype != 0x0B:
            continue
        peer = p.peer
        total[peer] += 1
        acked = False
        if i + 1 < len(timed):
            q = timed[i + 1]
            if (q.ptype == 0x0D and q.src_dev == p.dst_dev and q.src_room == p.dst_room
                    and q.dst_dev == p.src_dev and q.dst_room == p.src_room
                    and q.cmd == p.cmd and q.payload == p.payload):
                latencies[peer].append((q.ts - p.ts).total_seconds())
                acked = True
        if not acked:
            missing[peer] += 1

    if not total:
        r('  0x0B 전송 패킷이 없습니다.')
        return

    r('  ACK 판정: 직후 패킷이 주소를 뒤집고 커맨드·페이로드가 동일한 0x0D')
    r('')
    r(f'  {"장치":<22} {"방":<8} {"전송":>7} {"ACK":>7} {"미ACK":>7} {"ACK지연(중앙)":>13}')
    r(f'  {"-" * 22} {"-" * 8} {"-" * 7} {"-" * 7} {"-" * 7} {"-" * 13}')
    for peer, cnt in total.most_common(top):
        lat = latencies[peer]
        med = _fmt_sec(statistics.median(lat)) if lat else '-'
        r(f'  {dev_label(peer[0]):<22} {room_label(peer[1]):<8} {cnt:>7,} {len(lat):>7,} '
          f'{missing[peer]:>7,} {med:>13}')

    all_lat = [v for vs in latencies.values() for v in vs]
    if all_lat:
        r('')
        r(f'  전체 ACK 지연 : 중앙값 {_fmt_sec(statistics.median(all_lat))}, '
          f'최대 {_fmt_sec(max(all_lat))}')
        tot_missing = sum(missing.values())
        r(f'  미ACK 비율    : {tot_missing:,} / {sum(total.values()):,} '
          f'({tot_missing / sum(total.values()) * 100:.1f}%)')
        r('')
        r('  ⚠ EW11은 여러 프레임을 한 TCP 청크로 묶어 전달하므로 밀리초 미만의')
        r('    ACK 지연은 실제 버스 지연이 아니라 청크 경계의 산물이다.')


def section_state_changes(r: Report, packets: list[Pkt], top: int) -> None:
    r.head('6. 상태 변화 이벤트  —  장치가 보고한 페이로드가 바뀐 시점')

    last: dict[tuple[int, int, int], bytes] = {}
    events: list[tuple[datetime | None, tuple[int, int, int], bytes, bytes]] = []
    volatility: dict[tuple[int, int], list[set[int]]] = defaultdict(
        lambda: [set() for _ in range(8)]
    )

    for p in packets:
        # 장치가 스스로 보낸 실제 상태 보고(0x0B)만 본다.
        # ACK(0x0D)는 페이로드가 비어 있거나 상대 프레임의 반향이라
        # 이걸 섞으면 상태가 매번 바뀌는 것처럼 보인다.
        if p.from_wallpad or p.ptype != 0x0B:
            continue
        key = (p.src_dev, p.src_room, p.cmd)
        for i, b in enumerate(p.payload):
            volatility[(p.src_dev, p.src_room)][i].add(b)
        prev = last.get(key)
        if prev is not None and prev != p.payload:
            events.append((p.ts, key, prev, p.payload))
        last[key] = p.payload

    if not events:
        r('  캡처 구간 동안 장치 상태 변화가 관측되지 않았습니다 (완전 정지 상태).')
    else:
        r(f'  총 {len(events):,}건의 상태 변화')
        by_dev = Counter((k[0], k[1]) for _, k, _, _ in events)
        r('')
        for (dev, room), cnt in by_dev.most_common():
            r(f'    {dev_label(dev):<22} {room_label(room):<8} {cnt:>6,}건')
        r('')
        r(f'  [최근 변화 {min(top, len(events))}건]')
        for ts, key, prev, cur in events[-top:]:
            when = ts.strftime('%m-%d %H:%M:%S') if ts else '시각없음'
            diff = [i for i in range(8) if prev[i] != cur[i]]
            r(f'    {when}  {dev_label(key[0])} {room_label(key[1])} {cmd_label(key[2])}')
            r(f'      byte {diff} : {prev.hex(" ").upper()} → {cur.hex(" ").upper()}')
            if TR is not None:
                try:
                    for line in TR._decode_payload(key[0], key[1], key[2], cur, 0x0D, False):
                        r(f'      {line}')
                except Exception:
                    pass

    r('')
    r('  [페이로드 바이트별 변동성]  값의 가짓수 (1 = 캡처 내내 고정)')
    r(f'    {"장치":<22} {"방":<8} ' + ' '.join(f'B{i}' for i in range(8)))
    for (dev, room), buckets in sorted(volatility.items(), key=lambda kv: -sum(len(s) for s in kv[1])):
        counts = ' '.join(f'{len(s):>2}' for s in buckets)
        r(f'    {dev_label(dev):<22} {room_label(room):<8} {counts}')


def section_timeline(r: Report, packets: list[Pkt]) -> None:
    r.head('7. 시간대별 트래픽')

    timed = [p for p in packets if p.ts is not None]
    if len(timed) < 2:
        r('  시각 정보가 없어 생략합니다.')
        return

    per_min: Counter[datetime] = Counter()
    for p in timed:
        per_min[p.ts.replace(second=0, microsecond=0)] += 1

    counts = list(per_min.values())
    r(f'  분당 패킷 : 중앙값 {statistics.median(counts):.0f}, '
      f'최소 {min(counts)}, 최대 {max(counts)}')

    peak = max(counts)
    r('')
    r('  [10분 단위 추이]')
    per_10: Counter[datetime] = Counter()
    for minute, cnt in per_min.items():
        bucket = minute.replace(minute=minute.minute // 10 * 10)
        per_10[bucket] += cnt
    peak10 = max(per_10.values()) if per_10 else 1
    for bucket in sorted(per_10):
        cnt = per_10[bucket]
        bar = '█' * max(1, round(cnt / peak10 * 40))
        r(f'    {bucket:%m-%d %H:%M}  {cnt:>7,}  {bar}')

    quiet = [m for m, c in per_min.items() if c < peak * 0.2]
    if quiet:
        r('')
        r(f'  트래픽이 평소의 20% 미만인 분: {len(quiet)}개 '
          f'(통신 두절 또는 대기 구간 의심)')


# ── 진입점 ──────────────────────────────────────────────────────────
def main() -> None:
    ap = argparse.ArgumentParser(
        description='Kocom Wallpad RS485 패킷 로그 프로토콜 분석기',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            '예시:\n'
            '  python analyze-log.py capture.csv\n'
            '  python analyze-log.py capture.csv --out report.txt --top 30\n'
        ),
    )
    ap.add_argument('logfile', help='packet-log.py 가 남긴 로그 파일 (--csv 권장)')
    ap.add_argument('--out', metavar='PATH', help='리포트를 파일로도 저장')
    ap.add_argument('--top', type=int, default=20, help='표에 표시할 최대 항목 수 (기본: 20)')
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding='utf-8')  # type: ignore[union-attr]
    except Exception:
        pass

    path = Path(args.logfile)
    if not path.exists():
        raise SystemExit(f'오류: 파일을 찾을 수 없습니다 → {path}')

    packets, stats = load_packets(path)
    if not packets:
        raise SystemExit('오류: 유효한 패킷을 찾지 못했습니다.')

    r = Report()
    r('╔' + '═' * 76 + '╗')
    r('║  Kocom Wallpad RS485 프로토콜 분석 리포트')
    r(f'║  생성 시각: {datetime.now():%Y-%m-%d %H:%M:%S}')
    r('╚' + '═' * 76 + '╝')

    section_overview(r, packets, stats, path)
    section_traffic(r, packets)
    section_flows(r, packets, args.top)
    section_polling_cycle(r, packets)
    section_response(r, packets, args.top)
    section_state_changes(r, packets, args.top)
    section_timeline(r, packets)

    text = r.text()
    print(text)
    if args.out:
        Path(args.out).write_text(text + '\n', encoding='utf-8')
        print(f'\n리포트를 저장했습니다 → {Path(args.out).resolve()}')


if __name__ == '__main__':
    main()
