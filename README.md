# Home Assistant Addons

애드온 스토어에서 Repository URL을 https://github.com/iuscaler/Home-Assistant-Addons 으로 입력하고 애드온을 추가하세요.

## kocom-wallpad-rs485

> 본 애드온은 **푸르지오** 아파트(2025년 완공) 월패드 기준으로 작성되었습니다.

코콤 월패드 RS485 버스를 MQTT로 연결하는 Home Assistant 애드온입니다.  
시리얼 포트(USB-RS485) 또는 TCP 소켓(EW11 등 RS485-to-WiFi 장치) 연결을 지원하며,  
조명, 콘센트, 온도조절기, 에어컨, 환기장치, 가스밸브, 엘리베이터 등을 제어할 수 있습니다.

## kocom-wallpad-rs485-test-tool

`kocom-wallpad-rs485` 애드온 개발 및 디버깅을 위한 독립 실행형 Python 스크립트 모음입니다.  
Home Assistant 없이 PC에서 직접 RS485 버스를 분석할 수 있습니다.

자세한 사용 방법은 [kocom-wallpad-rs485-test-tool/README.md](kocom-wallpad-rs485-test-tool/README.md)를 참고하세요.

### 포함 도구

| 파일 | 설명 |
|------|------|
| `packet-log.py` | RS485 시리얼/소켓으로 수신되는 모든 패킷을 실시간 캡처하여 16진수로 출력 |
| `translate-packet.py` | 16진수 패킷을 stdin으로 입력받아 장치·방·커맨드·페이로드를 사람이 읽기 쉬운 형태로 해석 |

### 빠른 시작

```bash
# 소켓으로 연결된 RS485 패킷 실시간 캡처
python packet-log.py --type socket --host 192.168.1.100 --port 8899

# 패킷 캡처와 동시에 실시간 해석
python packet-log.py --type socket --host 192.168.1.100 | python translate-packet.py

# 단일 패킷 해석
echo "AA 55 30 BC 00 0E 00 01 00 3A 00 00 00 00 00 00 00 00 35 0D 0D" | python translate-packet.py
```
