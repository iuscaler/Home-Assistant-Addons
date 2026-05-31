# 변경 이력

## [2026-05-31]

### 추가
- 환기장치 CO₂ 농도 센서 Discovery 추가
- 환기장치 속도 조절 기능 추가 (약/중/강 3단계, HA 슬라이더 연동)
- 환기장치 꺼짐 예약 타이머 기능 추가 (0~12시간, 읽기 전용 표시)
- 다국어 번역 파일 추가 (`translations/en.yaml`, `translations/ko.yaml`)

### 변경
- 환기장치 켜기 시 기본 모드 auto, 기본 속도 0단계로 설정
- 취침(sleep) 모드 선택 시 속도 1단계(약풍) + 꺼짐 예약 8시간 자동 적용
- 설정 항목 `init_fan_mode` 제거 (환기장치 켜기 기본값 고정으로 불필요)
- 엔티티 이름 전체 한국어로 변경 (거실 조명, 방1 온도조절기 등)
- 방 이름 한국어 매핑 추가 (`거실`, `방1`~`방3`, `주방`)
- 공기질 센서 항목 중 온도·습도 레이블 한국어 적용

### 버그 수정
- 환기장치 프리셋 변경 시 속도 바이트가 0으로 초기화되던 문제 수정
- 환기장치 속도 3단계(강)가 Medium으로 잘못 매핑되던 문제 수정
- 환기장치 꺼짐 명령 시 미정의 변수(`preset`) 참조 오류 수정

---

## [2026-05-30]

### 추가
- kocom-wallpad 커스텀 컴포넌트의 패킷 파싱 로직 이식
  - `PacketFrame` 바이트 기반 파서 (기존 문자열 슬라이싱 대체)
  - 신규 장치 지원: 에어컨, 콘센트, 동작감지, 공기질(PM10/PM25/CO₂/VOC/온도/습도)
  - 엘리베이터 층수·방향 센서 추가
- 파일 분리: `const.py`, `models.py`, `transport.py`, `controller.py`, `discovery.py`
- HA Add-on Options UI 지원 (`config.json` options/schema 기반, `kocom.conf` 제거)
  - `devices` 항목을 오브젝트 리스트로 변경 (type/room 개별 선택)
  - 같은 방에 조명 여러 개 추가 시 번호 자동 부여 (단일은 번호 없음)
  - RS485 연결 방식 드롭다운 (`serial` / `socket`)
  - 엘리베이터 방식 드롭다운 (`rs485` / `tcpip`)

### 변경
- threading 기반 → `asyncio` 기반으로 전면 재작성
  - `paho-mqtt` → `aiomqtt` (async context manager, 자동 재연결)
  - `pyserial` → `pyserial-asyncio`
- 수신 루프 / 송신 큐 / 폴링 루프를 단일 이벤트 루프에서 실행
- MQTT 재연결 시 RS485 소켓 정상 해제 (`try/finally`)
- 온도조절기 방 코드 조회 버그 수정 (숫자 문자열 `'1'` → `int` 변환)

### 버그 수정
- `configparser` 미임포트로 인한 `NameError` 수정
- MQTT 자격증명 빈 문자열을 `None`으로 처리 (익명 접속 오동작 방지)
- `run.sh` Python 모듈 경로 문제 수정 (`/kocom.py` 직접 실행)

---

## [2026-05-30] - 최초 설정

### 추가
- [1saac-k/kocom.py](https://github.com/1saac-k/kocom.py) 소스를 기반으로 프로젝트 최초 설정
