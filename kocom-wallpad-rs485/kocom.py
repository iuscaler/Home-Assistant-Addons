#!/usr/bin/env python3
# -*- coding: utf-8 -*-

'''
 python kocom script

 : forked from script written by kyet, 룰루해피, 따분, Susu Daddy

 apt-get install mosquitto
 python3 -m pip install pyserial
 python3 -m pip install paho-mqtt
'''
import time
import platform
import threading
import queue
import random
import json
import paho.mqtt.client as mqtt
import logging
import configparser


# 상수 정의 -------------------------------
SW_VERSION = '2026.05.30'
CONFIG_FILE = 'kocom.conf'
BUF_SIZE = 100  # 수신 메시지 및 캐시 큐의 최대 크기

read_write_gap = 0.03  # 마지막 읽기 이후 쓰기까지 최소 대기 시간(초)
polling_interval = 300  # 장치 상태 폴링 주기(초)

# RS485 패킷 구조 상수
header_h = 'aa55'
trailer_h = '0d0d'
packet_size = 21       # 패킷 전체 크기 (21바이트)
chksum_position = 18   # 체크섬이 위치하는 바이트 번호

# 패킷 파싱용 코드 → 값 변환 딕셔너리 (수신 패킷 해석에 사용)
type_t_dic = {'30b':'send', '30d':'ack'}
seq_t_dic = {'c':1, 'd':2, 'e':3, 'f':4}
device_t_dic = {'01':'wallpad', '0e':'light', '2c':'gas', '36':'thermo', '3b': 'plug', '44':'elevator', '48':'fan'}
cmd_t_dic = {'00':'state', '01':'on', '02':'off', '3a':'query'}
room_t_dic = {'00':'livingroom', '01':'room1', '02':'room2', '03':'room3', '04':'kitchen'}

# 값 → 코드 역변환 딕셔너리 (송신 패킷 생성에 사용)
type_h_dic = {v: k for k, v in type_t_dic.items()}
seq_h_dic = {v: k for k, v in seq_t_dic.items()}
device_h_dic = {v: k for k, v in device_t_dic.items()}
cmd_h_dic = {v: k for k, v in cmd_t_dic.items()}
room_h_dic = {'livingroom':'00', 'room1':'01', 'room2':'02', 'room3':'03', 'kitchen':'04'}

# MQTT 함수 ----------------------------

def init_mqttc():
    # MQTT 클라이언트 초기화 및 브로커 연결 (최대 30회 재시도)
    mqttc = mqtt.Client()
    mqttc.on_message = mqtt_on_message
    mqttc.on_subscribe = mqtt_on_subscribe
    mqttc.on_connect = mqtt_on_connect
    mqttc.on_disconnect = mqtt_on_disconnect

    if config.get('MQTT','mqtt_allow_anonymous') != 'True':
        logtxt = "[MQTT] connecting (using username and password)"
        mqttc.username_pw_set(username=config.get('MQTT','mqtt_username',fallback=''), password=config.get('MQTT','mqtt_password',fallback=''))
    else:
        logtxt = "[MQTT] connecting (anonymous)"

    mqtt_server = config.get('MQTT','mqtt_server')
    mqtt_port = int(config.get('MQTT','mqtt_port'))
    for retry_cnt in range(1,31):
        try:
            logging.info(logtxt)
            mqttc.connect(mqtt_server, mqtt_port, 60)
            mqttc.loop_start()
            return mqttc
        except:
            logging.error('[MQTT] connection failure. #' + str(retry_cnt))
            time.sleep(10)
    return False

def mqtt_on_subscribe(mqttc, obj, mid, granted_qos):
    logging.info("[MQTT] Subscribed: " + str(mid) + " " + str(granted_qos))

def mqtt_on_log(mqttc, obj, level, string):
    logging.info("[MQTT] on_log : "+string)

def mqtt_on_connect(mqttc, userdata, flags, rc):
    # 연결 성공 시 kocom/# 토픽 전체 구독
    if rc == 0:
        logging.info("[MQTT] Connected - 0: OK")
        mqttc.subscribe('kocom/#', 0)
    else:
        logging.error("[MQTT] Connection error - {}: {}".format(rc, mqtt.connack_string(rc)))

def mqtt_on_disconnect(mqttc, userdata, rc=0):
    logging.error("[MQTT] Disconnected - "+str(rc))


# RS485 시리얼/소켓 통신 클래스 --------------------

class RS485Wrapper:
    def __init__(self, serial_port=None, socket_server=None, socket_port=0):
        # serial_port가 주어지면 시리얼, socket_server가 주어지면 소켓 모드로 동작
        if socket_server == None:
            self.type = 'serial'
            self.serial_port = serial_port
        else:
            self.type = 'socket'
            self.socket_server = socket_server
            self.socket_port = socket_port
        self.last_read_time = 0
        self.conn = False

    def connect(self):
        self.close()
        self.last_read_time = 0
        if self.type == 'serial':
            self.conn = self.connect_serial(self.serial_port)
        elif self.type == 'socket':
            self.conn = self.connect_socket(self.socket_server, self.socket_port)
        return self.conn

    def connect_serial(self, SERIAL_PORT):
        # 포트 미지정 시 OS에 따라 기본 포트 자동 선택
        if SERIAL_PORT == None:
            os_platfrom = platform.system()
            if os_platfrom == 'Linux':
                SERIAL_PORT = '/dev/ttyUSB0'
            else:
                SERIAL_PORT = 'com3'
        try:
            ser = serial.Serial(SERIAL_PORT, 9600, timeout=1)
            ser.bytesize = 8
            ser.stopbits = 1
            if not ser.is_open:
                raise Exception('Not ready')
            logging.info('[RS485] Serial connected : {}'.format(ser))
            return ser
        except Exception as e:
            logging.error('[RS485] Serial open failure : {}'.format(e))
            return False

    def connect_socket(self, SOCKET_SERVER, SOCKET_PORT):
        sock = socket.socket()
        sock.settimeout(10)
        try:
            sock.connect((SOCKET_SERVER, SOCKET_PORT))
        except Exception as e:
            logging.error('[RS485] Socket connection failure : {} | server {}, port {}'.format(e, SOCKET_SERVER, SOCKET_PORT))
            return False
        logging.info('[RS485] Socket connected | server {}, port {}'.format(SOCKET_SERVER, SOCKET_PORT))
        # 폴링 주기보다 약간 길게 타임아웃 설정하여 정상 무응답과 오류를 구분
        sock.settimeout(polling_interval+15)
        return sock

    def read(self):
        if self.conn is False:
            return ''
        ret = ''
        if self.type == 'serial':
            for _ in range(polling_interval+15):
                try:
                    ret = self.conn.read()
                except AttributeError:
                    raise Exception('exception occured while reading serial')
                except TypeError:
                    raise Exception('exception occured while reading serial')
                if len(ret) != 0:
                    break
        elif self.type == 'socket':
            ret = self.conn.recv(1)

        if len(ret) == 0:
            raise Exception('read byte errror')
        else:
            self.last_read_time = time.time()
        return ret

    def write(self, data):
        if self.conn is False:
            return False
        if self.last_read_time == 0:
            time.sleep(1)
        # 버스 충돌 방지: 마지막 수신 후 최소 간격을 보장한 뒤 송신
        while time.time() - self.last_read_time < read_write_gap:
            #logging.debug('pending write : time too short after last read')
            time.sleep(max([0, read_write_gap - time.time() + self.last_read_time]))
        if self.type == 'serial':
            return self.conn.write(data)
        elif self.type == 'socket':
            return self.conn.send(data)
        else:
            return False

    def close(self):
        ret = False
        if self.conn is not False:
            try:
                ret = self.conn.close()
                self.conn = False
            except:
                pass
        return ret

    def reconnect(self):
        # 연결이 끊어진 경우 성공할 때까지 10초 간격으로 재연결 시도
        self.close()
        while True:
            logging.info('[RS485] reconnecting to RS485...')
            if self.connect() is not False:
                break
            time.sleep(10)



def send(dest, src, cmd, value, log=None, check_ack=True):
    # ACK를 받지 못하면 시퀀스 코드를 바꿔가며 최대 4회 재전송
    send_lock.acquire()
    ack_data.clear()
    ret = False
    for seq_h in seq_t_dic.keys():
        payload = type_h_dic['send'] + seq_h + '00' + dest + src + cmd + value
        send_data = header_h + payload + chksum(payload) + trailer_h
        try:
            if rs485.write(bytearray.fromhex(send_data)) is False:
                raise Exception('Not ready')
        except Exception as ex:
            logging.error("[RS485] Write error.[{}]".format(ex) )
            break
        if log != None:
            logging.info('[SEND|{}] {}'.format(log, send_data))
        if not check_ack:
            time.sleep(1)
            ret = send_data
            break

        # ACK 대기 (1.3~1.5초 사이 랜덤 — 버스 충돌 감소 목적)
        ack_data.append(type_h_dic['ack'] + seq_h + '00' +  src + dest + cmd + value)
        try:
            ack_q.get(True, 1.3+0.2*random.random())
            if config.get('Log', 'show_recv_hex') == 'True':
                logging.info ('[ACK] OK')
            ret = send_data
            break
        except queue.Empty:
            pass

    if ret is False:
        logging.info('[RS485] send failed. closing RS485. it will try to reconnect to RS485 shortly.')
        rs485.close()
    ack_data.clear()
    send_lock.release()
    return ret


def chksum(data_h):
    # 페이로드 바이트 합계를 256으로 나눈 나머지를 2자리 16진수 문자열로 반환
    sum_buf = sum(bytearray.fromhex(data_h))
    return '{0:02x}'.format((sum_buf)%256)


# 수신 패킷 파싱 --------------------------------

def parse(hex_data):
    # RS485 수신 패킷(42자리 16진수 문자열)을 각 필드로 분해하여 딕셔너리로 반환
    header_h = hex_data[:4]       # 헤더 : aa55
    type_h = hex_data[4:7]        # 패킷 종류 : 30b(송신) 30d(응답)
    seq_h = hex_data[7:8]         # 시퀀스 번호 : c(1번) d(2번)
    monitor_h = hex_data[8:10]    # 모니터 주소 : 00(월패드) 02(주방TV)
    dest_h = hex_data[10:14]      # 목적지 주소 : 0100(월패드) 0e00(거실 조명) 3601(방1 온도조절기) 등
    src_h = hex_data[14:18]       # 송신지 주소
    cmd_h = hex_data[18:20]       # 명령 코드 : 3a(조회) 등
    value_h = hex_data[20:36]     # 데이터 값 (8바이트)
    chksum_h = hex_data[36:38]    # 체크섬
    trailer_h = hex_data[38:42]   # 트레일러

    data_h = hex_data[4:36]
    payload_h = hex_data[18:36]
    cmd = cmd_t_dic.get(cmd_h)

    ret = { 'header_h':header_h, 'type_h':type_h, 'seq_h':seq_h, 'monitor_h':monitor_h, 'dest_h':dest_h, 'src_h':src_h, 'cmd_h':cmd_h,
            'value_h':value_h, 'chksum_h':chksum_h, 'trailer_h':trailer_h, 'data_h':data_h, 'payload_h':payload_h,
            'type':type_t_dic.get(type_h),
            'seq':seq_t_dic.get(seq_h),
            'dest':device_t_dic.get(dest_h[:2]),
            'dest_subid':str(int(dest_h[2:4], 16)),
            'dest_room':room_t_dic.get(dest_h[2:4]),
            'src':device_t_dic.get(src_h[:2]),
            'src_subid':str(int(src_h[2:4], 16)),
            'src_room':room_t_dic.get(src_h[2:4]),
            'cmd':cmd if cmd!=None else cmd_h,
            'value':value_h,
            'time':time.time(),
            'flag':None}
    return ret


def thermo_parse(value):
    # 온도조절기 값 파싱: 난방 모드, 외출 여부, 설정 온도, 현재 온도
    ret = { 'heat_mode': 'heat' if value[:2] == '11' else 'off',
            'away': 'true' if value[2:4] == '01' else 'false',
            'set_temp': int(value[4:6], 16) if value[:2] == '11' else int(config.get('User', 'init_temp')),
            'cur_temp': int(value[8:10], 16)}
    return ret


def light_parse(value):
    # 조명 값 파싱: 설정된 조명 수만큼 각 조명의 on/off 상태를 딕셔너리로 반환
    ret = {}
    for i in range(1, int(config.get('User', 'light_count'))+1):
        ret['light_'+str(i)] = 'off' if value[i*2-2:i*2] == '00' else 'on'
    return ret


def fan_parse(value):
    # 환기장치 값 파싱: 전원 상태 및 풍량 단계
    preset_dic = {'40':'Low', '80':'Medium', 'c0':'High'}
    state = 'off' if value[:2] == '10' else 'on'
#    state = 'off' if value[:2] == '00' else 'on'
    preset = 'Off' if state == 'off' else preset_dic.get(value[4:6])
    return { 'state': state, 'preset': preset}


# 장치 상태 조회 --------------------------

def query(device_h, publish=False, enforce=False):
    global cache_data
    # 캐시에 유효한 데이터가 있으면 캐시를 반환하고, 없으면 RS485로 조회 패킷 전송
    for c in cache_data:
        if enforce: break
        if time.time() - c['time'] > polling_interval:  # 폴링 주기를 초과한 캐시는 무효 처리
            break
        if c['type'] == 'ack' and c['src'] == 'wallpad' and c['dest_h'] == device_h and c['cmd'] != 'query':
            if (config.get('Log', 'show_query_hex') == 'True'):
                logging.info('[cache|{}{}] query cache {}'.format(c['dest'], c['dest_subid'], c['data_h']))
            return c

    if (config.get('Log', 'show_query_hex') == 'True'):
        log = 'query ' + device_t_dic.get(device_h[:2]) + str(int(device_h[2:4],16))
    else:
        log = None
    return send_wait_response(dest=device_h, cmd=cmd_h_dic['query'], log=log, publish=publish)


def send_wait_response(dest, src=device_h_dic['wallpad']+'00', cmd=cmd_h_dic['state'], value='0'*16, log=None, check_ack=True, publish=True):
    # 패킷을 전송한 뒤 응답 패킷이 wait_q에 도착할 때까지 최대 2초 대기
    #logging.debug('waiting for send_wait_response :'+dest)
    wait_target.put(dest)
    #logging.debug('entered send_wait_response :'+dest)
    ret = { 'value':'0'*16, 'flag':False }

    if send(dest, src, cmd, value, log, check_ack) is not False:
        try:
            ret = wait_q.get(True, 2)
            if publish:
                publish_status(ret)
        except queue.Empty:
            pass
    wait_target.get()
    #logging.debug('exiting send_wait_response :'+dest)
    return ret


#===== TCP/IP 방식 엘리베이터 호출 =====

def call_elevator_tcpip():
    # 아파트 서버에 소켓으로 연결하여 순서대로 패킷을 교환하며 엘리베이터를 호출
    import socket
    sock = socket.socket()
    sock.settimeout(10)

    APT_SERVER = config.get('Elevator', 'tcpip_apt_server')
    APT_PORT = int(config.get('Elevator', 'tcpip_apt_port'))

    try:
        sock.connect((APT_SERVER, APT_PORT))
    except Exception as e:
        logging.error('Apartment server socket connection failure : {} | server {}, port {}'.format(e, APT_SERVER, APT_PORT))
        return False
    logging.info('Apartment server socket connected | server {}, port {}'.format(APT_SERVER, APT_PORT))

    try:
        sock.send(bytearray.fromhex(config.get('Elevator', 'tcpip_packet1')))
        rcv = sock.recv(512)
        logging.info('recv from apt server: '+''.join("%02x" % i for i in rcv) )
        time.sleep(0.1)
        sock.send(bytearray.fromhex(config.get('Elevator', 'tcpip_packet2')))
        rcv = sock.recv(512)
        logging.info('recv from apt server: '+''.join("%02x" % i for i in rcv) )
        sock.send(bytearray.fromhex(config.get('Elevator', 'tcpip_packet3')))
        for _ in range(100):
            rcv = sock.recv(512)
            if len(rcv) == 0:
                logging.info('apt server connection closed by peer')
                sock.close()
                return True
            rcv_hex = ''.join("%02x" % i for i in rcv)
            logging.info('recv from apt server: '+rcv_hex )
            if rcv_hex == config.get('Elevator', 'tcpip_packet4'):
                logging.info('elevator arrived. sending last heartbeat' )
                break
        sock.send(bytearray.fromhex(config.get('Elevator', 'tcpip_packet2')))
        rcv = sock.recv(512)
        logging.info('recv from apt server: '+''.join("%02x" % i for i in rcv) )
        sock.close()
    except Exception as e:
        logging.error('Apartment server socket communication failure : {}'.format(e))
        return False

    return True


#===== MQTT 수신 → RS485 패킷 전송 =====

def mqtt_on_message(mqttc, obj, msg):
    # MQTT command 토픽 수신 시 해당 장치에 RS485 제어 패킷 전송
    command = msg.payload.decode('ascii')
    topic_d = msg.topic.split('/')

    # command 토픽이 아닌 경우 무시
    if topic_d[-1] != 'command':
        return

    logging.info("[MQTT RECV] " + msg.topic + " " + str(msg.qos) + " " + str(msg.payload))

    # 온도조절기 난방 모드 변경 : kocom/room/thermo/3/heat_mode/command
    if 'thermo' in topic_d and 'heat_mode' in topic_d:
#        heatmode_dic = {'heat': '11', 'off': '01'}
        heatmode_dic = {'heat': '11', 'off': '00'}

        dev_id = device_h_dic['thermo']+'{0:02x}'.format(int(topic_d[3]))
        q = query(dev_id)
        #settemp_hex = q['value'][4:6] if q['flag']!=False else '14'
        settemp_hex = '{0:02x}'.format(int(config.get('User', 'init_temp'))) if q['flag']!=False else '14'
        value = heatmode_dic.get(command) + '00' + settemp_hex + '0000000000'
        send_wait_response(dest=dev_id, value=value, log='thermo heatmode')

    # 온도조절기 목표 온도 설정 : kocom/room/thermo/3/set_temp/command
    elif 'thermo' in topic_d and 'set_temp' in topic_d:
        dev_id = device_h_dic['thermo']+'{0:02x}'.format(int(topic_d[3]))
        settemp_hex = '{0:02x}'.format(int(float(command)))

        value = '1100' + settemp_hex + '0000000000'
        send_wait_response(dest=dev_id, value=value, log='thermo settemp')

    # 조명 on/off : kocom/livingroom/light/1/command
    elif 'light' in topic_d:
        dev_id = device_h_dic['light'] + room_h_dic.get(topic_d[1])
        value = query(dev_id)['value']
        onoff_hex = 'ff' if command == 'on' else '00'
        light_id = int(topic_d[3])

        # 조명 번호가 두 자리 이상이면 각 자릿수를 조명 번호로 해석하여 동시 제어
        # 예: kocom/livingroom/light/12/command → 1번, 2번 조명 동시 제어
        if light_id > 0:
            while light_id > 0:
                n = light_id % 10
                value = value[:n*2-2] + onoff_hex + value[n*2:]
                send_wait_response(dest=dev_id, value=value, log='light')
                light_id = int(light_id/10)
        else:
            send_wait_response(dest=dev_id, value=value, log='light')

    # 가스 차단 : kocom/livingroom/gas/command (off 명령만 허용)
    elif 'gas' in topic_d:
        dev_id = device_h_dic['gas'] + room_h_dic.get(topic_d[1])
        if command == 'off':
            send_wait_response(dest=dev_id, cmd=cmd_h_dic.get(command), log='gas')
        else:
            logging.info('You can only turn off gas.')

    # 엘리베이터 호출 : kocom/myhome/elevator/command
    elif 'elevator' in topic_d:
        dev_id = device_h_dic['elevator'] + room_h_dic.get(topic_d[1])
        state_on = json.dumps({'state': 'on'})
        state_off = json.dumps({'state': 'off'})
        if command == 'on':
            ret_elevator = None
            if config.get('Elevator', 'type', fallback='rs485') == 'rs485':
                ret_elevator = send(dest=device_h_dic['wallpad']+'00', src=dev_id, cmd=cmd_h_dic['on'], value='0'*16, log='elevator', check_ack=False)
            elif config.get('Elevator', 'type', fallback='rs485') == 'tcpip':
                ret_elevator = call_elevator_tcpip()

            if ret_elevator is False:
                logging.debug('elevator send failed')
                return

            threading.Thread(target=mqttc.publish, args=("kocom/myhome/elevator/state", state_on)).start()
            # rs485_floor 미설정 시 5초 후 자동으로 off 상태 발행
            if config.get('Elevator', 'rs485_floor', fallback=None) == None:
                threading.Timer(5, mqttc.publish, args=("kocom/myhome/elevator/state", state_off)).start()

        elif command == 'off':
            threading.Thread(target=mqttc.publish, args=("kocom/myhome/elevator/state", state_off)).start()

    # 환기장치 풍량 설정 : kocom/livingroom/fan/set_preset_mode/command
    elif 'fan' in topic_d and 'set_preset_mode' in topic_d:
        dev_id = device_h_dic['fan'] + room_h_dic.get(topic_d[1])
        onoff_dic = {'off':'1000', 'on':'1100'}  #onoff_dic = {'off':'0000', 'on':'1101'}
        speed_dic = {'Off':'00', 'Low':'40', 'Medium':'80', 'High':'c0'}
        if command == 'Off':
            onoff = onoff_dic['off']
        elif command in speed_dic.keys():
            onoff = onoff_dic['on']

        speed = speed_dic.get(command)
        value = onoff + speed + '0'*10
        send_wait_response(dest=dev_id, value=value, log='fan')

    # 환기장치 on/off : kocom/livingroom/fan/command
    elif 'fan' in topic_d:
        dev_id = device_h_dic['fan'] + room_h_dic.get(topic_d[1])
        onoff_dic = {'off':'1000', 'on':'1100'}  #onoff_dic = {'off':'0000', 'on':'1101'}
        speed_dic = {'Low':'40', 'Medium':'80', 'High':'c0'}
        init_fan_mode = config.get('User', 'init_fan_mode')
        if command in onoff_dic.keys():
            onoff = onoff_dic.get(command)
            speed = speed_dic.get(init_fan_mode)  #value = query(dev_id)['value']  #speed = value[4:6]

        value = onoff + speed + '0'*10
        send_wait_response(dest=dev_id, value=value, log='fan')

    # 수동 전체 조회 : kocom/myhome/query/command
    elif 'query' in topic_d:
        if command == 'PRESS':
            poll_state(enforce=True)


#===== RS485 수신 패킷 → MQTT 발행 =====

def publish_status(p):
    # 패킷 처리를 별도 스레드에서 실행하여 수신 루프 블로킹 방지
    threading.Thread(target=packet_processor, args=(p,)).start()

def packet_processor(p):
    logtxt = ""
    if p['type'] == 'send' and p['dest'] == 'wallpad':  # 월패드로 향하는 응답 패킷
        if p['src'] == 'thermo' and p['cmd'] == 'state':
            state = thermo_parse(p['value'])
            logtxt='[MQTT publish|thermo] id[{}] data[{}]'.format(p['src_subid'], state)
            mqttc.publish("kocom/room/thermo/" + p['src_subid'] + "/state", json.dumps(state))
        elif p['src'] == 'light' and p['cmd'] == 'state':
            state = light_parse(p['value'])
            logtxt='[MQTT publish|light] room[{}] data[{}]'.format(p['src_room'], state)
            mqttc.publish("kocom/{}/light/state".format(p['src_room']), json.dumps(state))
        elif p['src'] == 'fan' and p['cmd'] == 'state':
            state = fan_parse(p['value'])
            logtxt='[MQTT publish|fan] data[{}]'.format(state)
            mqttc.publish("kocom/livingroom/fan/state", json.dumps(state))
        elif p['src'] == 'gas':
            state = {'state': p['cmd']}
            logtxt='[MQTT publish|gas] data[{}]'.format(state)
            mqttc.publish("kocom/livingroom/gas/state", json.dumps(state))
    elif p['type'] == 'send' and p['dest'] == 'elevator':
        floor = int(p['value'][2:4],16)
        rs485_floor = int(config.get('Elevator','rs485_floor', fallback=0))
        if rs485_floor != 0 :
            state = {'floor': floor}
            if rs485_floor == floor:
                state['state'] = 'off'
        else:
            state = {'state': 'off'}
        logtxt='[MQTT publish|elevator] data[{}]'.format(state)
        mqttc.publish("kocom/myhome/elevator/state", json.dumps(state))
        # aa5530bc0044000100010300000000000000350d0d

    if logtxt != "" and config.get('Log', 'show_mqtt_publish') == 'True':
        logging.info(logtxt)


#===== Home Assistant MQTT 디스커버리 발행 =====

def discovery():
    # kocom.conf의 enabled 목록을 순회하며 각 장치의 디스커버리 정보 발행
    dev_list = [x.strip() for x in config.get('Device','enabled').split(',')]
    for t in dev_list:
        dev = t.split('_')
        sub = ''
        if len(dev) > 1:
            sub = dev[1]
        publish_discovery(dev[0], sub)
    publish_discovery('query')

# Home Assistant MQTT 디스커버리 규격: <discovery_prefix>/<component>/<object_id>/config
# 참고: https://www.home-assistant.io/docs/mqtt/discovery/
def publish_discovery(dev, sub=''):
    if dev == 'fan':
        topic = 'homeassistant/fan/kocom_wallpad_fan/config'
        payload = {
            'name': 'Kocom Wallpad Fan',
            'cmd_t': 'kocom/livingroom/fan/command',
            'stat_t': 'kocom/livingroom/fan/state',
            'stat_val_tpl': '{{ value_json.state }}',
            'pr_mode_stat_t': 'kocom/livingroom/fan/state',
            'pr_mode_val_tpl': '{{ value_json.preset }}',
            'pr_mode_cmd_t': 'kocom/livingroom/fan/set_preset_mode/command',
            'pr_mode_cmd_tpl': '{{ value }}',
            'pr_modes': ['Off', 'Low', 'Medium', 'High'],
            'pl_on': 'on',
            'pl_off': 'off',
            'qos': 0,
            'uniq_id': '{}_{}_{}'.format('kocom', 'wallpad', dev),
            'device': {
                'name': '코콤 스마트 월패드',
                'ids': 'kocom_smart_wallpad',
                'mf': 'KOCOM',
                'mdl': '스마트 월패드',
                'sw': SW_VERSION
            }
        }
        logtxt='[MQTT Discovery|{}] data[{}]'.format(dev, topic)
        mqttc.publish(topic, json.dumps(payload))
        if logtxt != "" and config.get('Log', 'show_mqtt_publish') == 'True':
            logging.info(logtxt)
    elif dev == 'gas':
        topic = 'homeassistant/switch/kocom_wallpad_gas/config'
        payload = {
            'name': 'Kocom Wallpad Gas',
            'cmd_t': 'kocom/livingroom/gas/command',
            'stat_t': 'kocom/livingroom/gas/state',
            'val_tpl': '{{ value_json.state }}',
            'pl_on': 'on',
            'pl_off': 'off',
            'ic': 'mdi:gas-cylinder',
            'qos': 0,
            'uniq_id': '{}_{}_{}'.format('kocom', 'wallpad', dev),
            'device': {
                'name': '코콤 스마트 월패드',
                'ids': 'kocom_smart_wallpad',
                'mf': 'KOCOM',
                'mdl': '스마트 월패드',
                'sw': SW_VERSION
            }
        }
        logtxt='[MQTT Discovery|{}] data[{}]'.format(dev, topic)
        mqttc.publish(topic, json.dumps(payload))
        if logtxt != "" and config.get('Log', 'show_mqtt_publish') == 'True':
            logging.info(logtxt)
    elif dev == 'elevator':
        topic = 'homeassistant/switch/kocom_wallpad_elevator/config'
        payload = {
            'name': 'Kocom Wallpad Elevator',
            'cmd_t': "kocom/myhome/elevator/command",
            'stat_t': "kocom/myhome/elevator/state",
            'val_tpl': "{{ value_json.state }}",
            'pl_on': 'on',
            'pl_off': 'off',
            'ic': 'mdi:elevator',
            'qos': 0,
            'uniq_id': '{}_{}_{}'.format('kocom', 'wallpad', dev),
            'device': {
                'name': '코콤 스마트 월패드',
                'ids': 'kocom_smart_wallpad',
                'mf': 'KOCOM',
                'mdl': '스마트 월패드',
                'sw': SW_VERSION
            }
        }
        logtxt='[MQTT Discovery|{}] data[{}]'.format(dev, topic)
        mqttc.publish(topic, json.dumps(payload))
        if logtxt != "" and config.get('Log', 'show_mqtt_publish') == 'True':
            logging.info(logtxt)
    elif dev == 'light':
        for num in range(1, int(config.get('User', 'light_count'))+1):
            #ha_topic = 'homeassistant/light/kocom_livingroom_light1/config'
            topic = 'homeassistant/light/kocom_{}_light{}/config'.format(sub, num)
            payload = {
                'name': 'Kocom {} Light{}'.format(sub, num),
                'cmd_t': 'kocom/{}/light/{}/command'.format(sub, num),
                'stat_t': 'kocom/{}/light/state'.format(sub),
                'stat_val_tpl': '{{ value_json.light_' + str(num) + ' }}',
                'pl_on': 'on',
                'pl_off': 'off',
                'qos': 0,
                'uniq_id': '{}_{}_{}{}'.format('kocom', 'wallpad', dev, num),
                'device': {
                    'name': '코콤 스마트 월패드',
                    'ids': 'kocom_smart_wallpad',
                    'mf': 'KOCOM',
                    'mdl': '스마트 월패드',
                    'sw': SW_VERSION
                }
            }
            logtxt='[MQTT Discovery|{}{}] data[{}]'.format(dev, num, topic)
            mqttc.publish(topic, json.dumps(payload))
            if logtxt != "" and config.get('Log', 'show_mqtt_publish') == 'True':
                logging.info(logtxt)
    elif dev == 'thermo':
        num = int(room_h_dic.get(sub))
        #ha_topic = 'homeassistant/climate/kocom_livingroom_thermostat/config'
        topic = 'homeassistant/climate/kocom_{}_thermostat/config'.format(sub)
        payload = {
            'name': 'Kocom {} Thermostat'.format(sub),
            'mode_cmd_t': 'kocom/room/thermo/{}/heat_mode/command'.format(num),
            'mode_stat_t': 'kocom/room/thermo/{}/state'.format(num),
            'mode_stat_tpl': '{{ value_json.heat_mode }}',

            'temp_cmd_t': 'kocom/room/thermo/{}/set_temp/command'.format(num),
            'temp_stat_t': 'kocom/room/thermo/{}/state'.format(num),
            'temp_stat_tpl': '{{ value_json.set_temp }}',

            'curr_temp_t': 'kocom/room/thermo/{}/state'.format(num),
            'curr_temp_tpl': '{{ value_json.cur_temp }}',
            'modes': ['off', 'heat'],
            'min_temp': 20,
            'max_temp': 30,
            'ret': 'false',
            'qos': 0,
            'uniq_id': '{}_{}_{}{}'.format('kocom', 'wallpad', dev, num),
            'device': {
                'name': '코콤 스마트 월패드',
                'ids': 'kocom_smart_wallpad',
                'mf': 'KOCOM',
                'mdl': '스마트 월패드',
                'sw': SW_VERSION
            }
        }
        logtxt='[MQTT Discovery|{}{}] data[{}]'.format(dev, num, topic)
        mqttc.publish(topic, json.dumps(payload))
        if logtxt != "" and config.get('Log', 'show_mqtt_publish') == 'True':
            logging.info(logtxt)
    elif dev == 'query':
        topic = 'homeassistant/button/kocom_wallpad_query/config'
        payload = {
            'name': 'Kocom Wallpad Query',
            'cmd_t': 'kocom/myhome/query/command',
            'qos': 0,
            'uniq_id': '{}_{}_{}'.format('kocom', 'wallpad', dev),
            'device': {
                'name': '코콤 스마트 월패드',
                'ids': 'kocom_smart_wallpad',
                'mf': 'KOCOM',
                'mdl': '스마트 월패드',
                'sw': SW_VERSION
            }
        }
        logtxt='[MQTT Discovery|{}] data[{}]'.format(dev, topic)
        mqttc.publish(topic, json.dumps(payload))
        if logtxt != "" and config.get('Log', 'show_mqtt_publish') == 'True':
            logging.info(logtxt)


#===== 스레드 함수 =====

def poll_state(enforce=False):
    # enabled 장치 목록을 순회하며 상태를 조회하고, 완료 후 다음 폴링 타이머 재설정
    global poll_timer
    poll_timer.cancel()

    dev_list = [x.strip() for x in config.get('Device','enabled').split(',')]
    no_polling_list = ['wallpad', 'elevator']  # 폴링 불필요한 장치 제외

    # 스레드 생존 여부 확인 후 죽어있으면 재시작
    for thread_instance in thread_list:
        if not thread_instance.is_alive():
            logging.error('[THREAD] {} is not active. starting.'.format( thread_instance.name))
            thread_instance.start()

    for t in dev_list:
        dev = t.split('_')
        if dev[0] in no_polling_list:
            continue

        dev_id = device_h_dic.get(dev[0])
        if len(dev) > 1:
            sub_id = room_h_dic.get(dev[1])
        else:
            sub_id = '00'

        if dev_id != None and sub_id != None:
            if query(dev_id + sub_id, publish=True, enforce=enforce)['flag'] is False:
                break
            time.sleep(1)

    poll_timer.cancel()
    poll_timer = threading.Timer(polling_interval, poll_state)
    poll_timer.start()


def read_serial():
    # RS485 버스에서 1바이트씩 읽어 유효한 패킷을 조립한 뒤 msg_q에 삽입
    global poll_timer, cache_data
    buf = ''
    not_parsed_buf = ''
    while True:
        try:
            d = rs485.read()
            hex_d = '{0:02x}'.format(ord(d))

            buf += hex_d
            # 헤더와 불일치하는 데이터는 not_parsed_buf로 이동 후 다음 헤더 탐색
            if buf[:len(header_h)] != header_h[:len(buf)]:
                not_parsed_buf += buf
                buf=''
                frame_start = not_parsed_buf.find(header_h, len(header_h))
                if frame_start < 0:
                    continue
                else:
                    not_parsed_buf = not_parsed_buf[:frame_start]
                    buf = not_parsed_buf[frame_start:]

            if not_parsed_buf != '':
                logging.info('[comm] not parsed '+not_parsed_buf)
                not_parsed_buf = ''

            if len(buf) == (packet_size * 2):
                chksum_calc = chksum(buf[len(header_h):chksum_position*2])
                chksum_buf = buf[chksum_position*2:chksum_position*2+2]
                if chksum_calc == chksum_buf and buf[-len(trailer_h):] == trailer_h:
                    if msg_q.full():
                        logging.error('msg_q is full. probably error occured while running listen_hexdata thread. please manually restart the program.')
                    msg_q.put(buf)  # 유효한 패킷을 큐에 삽입
                    buf=''
                else:
                    logging.info("[comm] invalid packet {} expected checksum {}".format(buf, chksum_calc))
                    # 잘못된 패킷 내부에 헤더가 있으면 해당 위치부터 재파싱
                    frame_start = buf.find(header_h, len(header_h))
                    if frame_start < 0:
                        not_parsed_buf += buf
                        buf=''
                    else:
                        not_parsed_buf += buf[:frame_start]
                        buf = buf[frame_start:]
        except Exception as ex:
            logging.error("*** Read error.[{}]".format(ex) )
            poll_timer.cancel()
            del cache_data[:]
            rs485.reconnect()
            poll_timer = threading.Timer(2, poll_state)
            poll_timer.start()


def listen_hexdata():
    global cache_data
    # msg_q에서 패킷을 꺼내 파싱 후 캐시 저장 및 ACK/대기 응답 처리
    while True:
        d = msg_q.get()

        if config.get('Log', 'show_recv_hex') == 'True':
            logging.info("[recv] " + d)

        p_ret = parse(d)

        # 최근 패킷을 캐시 앞에 삽입하고 BUF_SIZE 초과분 제거
        cache_data.insert(0, p_ret)
        if len(cache_data) > BUF_SIZE:
            del cache_data[-1]

        # ACK 대기 중인 패킷이면 ack_q에 전달하고 다음 패킷 처리
        if p_ret['data_h'] in ack_data:
            ack_q.put(d)
            continue

        # send_wait_response가 기다리는 응답이면 wait_q에 전달
        if not wait_target.empty():
            if p_ret['dest_h'] == wait_target.queue[0] and p_ret['type'] == 'ack':
            #if p_ret['src_h'] == wait_target.queue[0] and p_ret['type'] == 'send':
                if len(ack_data) != 0:
                    logging.info("[ACK] No ack received, but responce packet received before ACK. Assuming ACK OK")
                    ack_q.put(d)
                    time.sleep(0.5)
                wait_q.put(p_ret)
                continue
        publish_status(p_ret)


#========== 메인 ==========

if __name__ == "__main__":
    logging.basicConfig(format='%(levelname)s[%(asctime)s]:%(message)s ', level=logging.DEBUG)

    # 설정 파일 로드
    config = configparser.ConfigParser()
    config.read(CONFIG_FILE)

    # RS485 연결 초기화 (시리얼 또는 소켓)
    if config.get('RS485', 'type') == 'serial':
        import serial
        rs485 = RS485Wrapper(serial_port = config.get('RS485', 'serial_port', fallback=None))
    elif config.get('RS485', 'type') == 'socket':
        import socket
        rs485 = RS485Wrapper(socket_server = config.get('RS485', 'socket_server'), socket_port = int(config.get('RS485', 'socket_port')))
    else:
        logging.error('[CONFIG] invalid type value in [RS485]: only "serial" or "socket" is allowed. exit')
        exit(1)
    if rs485.connect() is False:
        logging.error('[RS485] connection error. exit')
        exit(1)

    # MQTT 브로커 연결
    mqttc = init_mqttc()
    if mqttc is False:
        logging.error('[MQTT] conection error. exit')
        exit(1)

    # 큐 및 동기화 객체 초기화
    msg_q = queue.Queue(BUF_SIZE)   # RS485 수신 패킷 큐
    ack_q = queue.Queue(1)          # ACK 수신 알림 큐
    ack_data = []                   # 현재 ACK 대기 중인 패킷 목록
    wait_q = queue.Queue(1)         # send_wait_response 응답 수신 큐
    wait_target = queue.Queue(1)    # 현재 응답 대기 중인 목적지 주소
    send_lock = threading.Lock()    # RS485 송신 직렬화 락
    poll_timer = threading.Timer(1, poll_state)  # 1초 후 첫 폴링 시작

    cache_data = []  # 수신 패킷 캐시 (최대 BUF_SIZE개)

    # 수신 스레드 시작
    thread_list = []
    thread_list.append(threading.Thread(target=read_serial, name='read_serial'))
    thread_list.append(threading.Thread(target=listen_hexdata, name='listen_hexdata'))
    for thread_instance in thread_list:
        thread_instance.start()

    poll_timer.start()

    # Home Assistant 디스커버리 정보 발행
    discovery()
