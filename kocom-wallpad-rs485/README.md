# 코콤 스마트 월패드 RS485

![Supports aarch64 Architecture][aarch64-shield]
![Supports amd64 Architecture][amd64-shield]
![Supports armhf Architecture][armhf-shield]
![Supports armv7 Architecture][armv7-shield]
![Supports i386 Architecture][i386-shield]

코콤(KOCOM) 스마트 월패드를 RS485 버스를 통해 Home Assistant와 연동하는 애드온입니다.  
RS485 신호를 MQTT로 브리징하여 HA에서 직접 장치를 제어하고 상태를 확인할 수 있습니다.

---

## 지원 장치

| 장치 | 기능 |
|---|---|
| 조명 | 켜기/끄기, 방별 개수 설정 |
| 콘센트 | 켜기/끄기 |
| 난방 온도조절기 | 난방 모드, 목표 온도 설정 |
| 에어컨 | 운전 모드, 풍량, 온도 설정 |
| 환기장치 | 켜기/끄기, 운전 모드, 속도 3단계, 꺼짐 예약, CO₂ 표시 |
| 가스밸브 | 차단 (끄기 전용) |
| 엘리베이터 | 호출, 층수·방향 표시 |
| 동작감지 센서 | 감지 여부 |
| 공기질 센서 | PM10, PM2.5, CO₂, VOC, 온도, 습도 |

---

## 설치

1. HA 웹UI → **설정 → 애드온 → 애드온 스토어** → 우측 상단 ⋮ → **저장소 추가**
2. 저장소 URL 입력 후 추가:
   ```
   https://github.com/iuscaler/kocom_wallpad_app_for_homeassistant
   ```
3. 애드온 스토어 하단의 **Kocom Wallpad with RS485** 클릭 → **설치**
4. 설치 완료 후 **설정** 탭에서 환경에 맞게 옵션 구성
5. **시작** 버튼으로 실행

---

## 설정

### RS485 연결

| 항목 | 설명 |
|---|---|
| **RS485 연결 방식** | `serial`: USB-RS485 어댑터 직접 연결 / `socket`: Elfin EW11 등 네트워크 어댑터 |
| **시리얼 포트 경로** | 연결 방식이 `serial`일 때 사용 (예: `/dev/ttyUSB0`) |
| **소켓 서버 주소** | 연결 방식이 `socket`일 때 네트워크 어댑터 IP (예: `192.168.1.100`) |
| **소켓 포트** | 네트워크 어댑터 포트 번호 (예: `8899`) |

### MQTT

| 항목 | 설명 |
|---|---|
| **MQTT 서버 주소** | MQTT 브로커 IP. HA 내장 브로커는 `172.30.32.1` |
| **MQTT 포트** | 기본값 `1883` |
| **익명 접속 허용** | 자격증명 없이 접속 가능한 경우 활성화 |
| **MQTT 아이디 / 비밀번호** | 익명 접속 비활성화 시 입력 |

### 장치 목록

`devices` 항목에서 제어할 장치를 추가합니다.

- **type**: 장치 종류 선택 (`light`, `outlet`, `fan`, `thermo`, `aircon`, `gas`, `elevator`, `motion`, `airquality`)
- **room**: 방 선택 (`livingroom`, `room1`, `room2`, `room3`, `kitchen`)

같은 방에 조명이 여러 개라면 같은 항목을 여러 번 추가하면 번호가 자동 부여됩니다.  
예) `light / livingroom` 2번 추가 → **거실 조명 1**, **거실 조명 2**

### 기타

| 항목 | 설명 |
|---|---|
| **엘리베이터 방식** | `rs485`: RS485 버스 호출 / `tcpip`: 아파트 서버 직접 연결 (고급) |
| **거주 층수** | 엘리베이터 도착 감지에 사용. `0`이면 비활성화 |
| **난방 기본 온도** | 이전 설정 없이 난방 켤 때 적용되는 온도 (°C) |
| **RS485 수신 패킷 로그** | 디버깅 시 활성화 |
| **MQTT 발행 로그** | 디버깅 시 활성화 |

---

## 참고

- 이 애드온은 로컬에서 Docker 이미지를 직접 빌드하므로 설치에 수 분이 소요될 수 있습니다.
- Mosquitto 브로커 애드온과 함께 사용하는 경우 MQTT 서버 주소는 `172.30.32.1`을 입력하세요.
- 설정 변경 후에는 애드온을 **재시작**해야 반영됩니다.

---

## 기반 프로젝트

이 애드온은 아래 오픈소스 프로젝트의 코드를 바탕으로 제작되었습니다.

- [1saac-k/kocom.py](https://github.com/1saac-k/kocom.py) — RS485 통신 및 MQTT 브리징 로직
- [lunDreame/kocom-wallpad](https://github.com/lunDreame/kocom-wallpad) — 패킷 파싱 및 장치 제어 로직

---

[aarch64-shield]: https://img.shields.io/badge/aarch64-yes-green.svg
[amd64-shield]: https://img.shields.io/badge/amd64-yes-green.svg
[armhf-shield]: https://img.shields.io/badge/armhf-yes-green.svg
[armv7-shield]: https://img.shields.io/badge/armv7-yes-green.svg
[i386-shield]: https://img.shields.io/badge/i386-yes-green.svg
