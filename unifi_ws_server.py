import json
import logging
import os
import ssl
import time
import uuid
import yaml

import asyncio
import websockets


STATE = {
    'messageId': 123,
}


def make_message(functionName, responseExpected=False,
                 payload=None, inResponseTo=0):
    messageId = STATE['messageId']
    STATE['messageId'] += 1
    message = {
        'from': 'UniFiVideo',
        'to': 'ubnt_avclient',
        'responseExpected': responseExpected,
        'functionName': functionName,
        'messageId': messageId,
        'inResponseTo': inResponseTo,
        'payload': payload,
    }
    return message


class NoSuchCamera(Exception):
    pass


class CameraState(object):
    def load_config(self):
        if os.path.exists(self.conf_file):
            with open(self.conf_file) as f:
                self.conf = yaml.load(f.read())
        else:
            self.log.info('No config file for camera: %s' % self.conf_file)
            self.conf = {}
        self.needs_config_reload = False

    def __init__(self, client_info, websocket, conf_file):
        self.client_info = client_info
        self.websocket = websocket
        self.conf_file = conf_file
        self.camera_mac = client_info['mac']
        self.conf = {}
        self.streams = {}
        self.needs_config_reload = True
        self.last_time_sync = 0
        self.time_sync_interval = 120


class WebSocketWrapper(object):
    def __init__(self, websocket):
        self.websocket = websocket
        self.msgq = []

    def recv_from_queue(self, inResponseTo=None):
        if inResponseTo is None:
            if self.msgq:
                return self.msgq.pop()
        else:
            for i, msg in enumerate(reversed(self.msgq)):
                if inResponseTo == msg['inResponseTo']:
                    del self.msgq[i]
                    return msg

    @asyncio.coroutine
    def recv(self, inResponseTo=None, blocking=True):
        while True:
            msg = self.recv_from_queue(inResponseTo=inResponseTo)
            if msg:
                return msg
            if blocking:
                msg = yield from self.websocket.recv()
            else:
                msg = self.websocket.messages.get_nowait()

            if msg:
                self.msgq.insert(0, json.loads(msg.decode()))
            else:
                raise Exception('client gone?')

    @asyncio.coroutine
    def send(self, msg):
        yield from self.websocket.send(msg)


class UVCWebsocketServer(object):
    def __init__(self, log=None, confdir='.'):
        self._cameras = {}
        self._msgq = []
        self.log = log or logging.getLogger('websocket')
        self.confdir = confdir

    def make_server(self, port, listen='0.0.0.0'):
        context = ssl.SSLContext(ssl.PROTOCOL_SSLv23)
        context.load_cert_chain('/etc/ssl/certs/ssl-cert-snakeoil.pem',
                                '/etc/ssl/private/ssl-cert-snakeoil.key')
        self.log.info('UVC management server started on port %i' % port)
        return websockets.serve(self.handle_camera,
                                listen, port, ssl=context)

    def reload_all_configs(self):
        for state in self._cameras.values():
            self.log.info('Scheduling config reload for %s' % state.camera_mac)
            state.needs_config_reload = True

    def is_camera_managed(self, camera_mac):
        return camera_mac in self._cameras

    @asyncio.coroutine
    def start_video(self, camera_mac, host, port):
        try:
            camera_state = self._cameras[camera_mac]
        except KeyError:
            raise NoSuchCamera()

        yield from self._start_video(camera_state, host, port)
        camera_state.host = host
        camera_state.port = port

    @asyncio.coroutine
    def stop_video(self, camera_mac):
        try:
            camera_state = self._cameras[camera_mac]
        except KeyError:
            raise NoSuchCamera()

        yield from self._stop_video(camera_state)

    @asyncio.coroutine
    def handle_camera(self, websocket, path):
        websocket = WebSocketWrapper(websocket)
        client_info = yield from self.do_hello(websocket)

        conf_file = os.path.join(
            self.confdir, '%s.yaml' % client_info['mac'])

        camera_state = CameraState(client_info, websocket, conf_file)
        self._cameras[client_info['mac']] = camera_state

        self.log.info(
            'Camera `%(name)s` now managed: %(mac)s @ %(ip)s' % client_info)

        yield from self.do_auth(websocket, 'ubnt', 'ubnt')
        yield from self._stop_video(camera_state)
        self.log.debug('Entering loop for camera %s' % client_info['mac'])
        yield from self.set_params(camera_state)
        while True:
            if camera_state.needs_config_reload:
                camera_state.load_config()
                yield from self.set_osd(camera_state)
                yield from self.set_isp(camera_state)
                yield from self.set_system(camera_state)
                yield from self.reconfig_streams(camera_state)

            yield from self.heartbeat(camera_state)
            yield from self.process_status(camera_state)
            yield from asyncio.sleep(5)

    @asyncio.coroutine
    def read_from_client(self, websocket, inResponseTo=None):
        result = yield from websocket.recv(inResponseTo=inResponseTo)
        self.log.debug('C->S: %s' % result)
        return result

    @asyncio.coroutine
    def send_to_client(self, websocket, data, really=True):
        self.log.debug('S->C: %s' % data)
        _ = yield from websocket.send(json.dumps(data))
        if data['responseExpected'] and really:
            response = yield from self.read_from_client(
                websocket, inResponseTo=data['messageId'])
            return response

    @asyncio.coroutine
    def do_hello(self, websocket):
        self.log.debug('Waiting for client hello')
        client_info = yield from self.read_from_client(websocket)
        msg = make_message('ubnt_avclient_hello',
                           payload={'protocolVersion': 25,
                                    'controllerName': 'unifi_video'},
                           inResponseTo=client_info['messageId'])
        self.log.debug('Sending server hello')
        yield from self.send_to_client(websocket, msg)
        return client_info['payload']

    @asyncio.coroutine
    def set_osd(self, camera_state):
        payload = {}
        settings = {
            'enableDate': 1,
            'enableLogo': 1,
            'tag': '',
            }
        for i in range(1, 5):
            try:
                stream_config = camera_state.conf['osd']['stream%i' % i]
            except KeyError:
                stream_config = {}
            payload['_%i' % i] = {k: stream_config.get(k, d) for
                                  k, d in settings.items()}
        msg = make_message('ChangeOsdSettings',
                           payload=payload,
                           responseExpected=True)
        yield from self.send_to_client(camera_state.websocket, msg)

    @asyncio.coroutine
    def set_isp(self, camera_state):
        settings = {
            'aeMode': 'auto',
            'brightness': 50,
            'contrast': 50,
            'denoise': 50,
            'flip': 0,
            'focusMode': 'ztrig',
            'focusPosition': 0,
            'hue': 50,
            'irLedLevel': 215,
            'irLedMode': 'auto',
            'mirror': 0,
            'saturation': 50,
            'sharpness': 50,
            'wdr': 1,
            'zoomPosition': 0,
        }
        try:
            isp_config = camera_state.conf['isp']
        except KeyError:
            isp_config = {}
        payload = {k: isp_config.get(k, d)
                   for k, d in settings.items()}
        msg = make_message('ChangeIspSettings',
                           payload=payload,
                           responseExpected=True)
        yield from self.send_to_client(camera_state.websocket, msg)

    @asyncio.coroutine
    def set_system(self, camera_state):
        camera_config = camera_state.conf.get('camera', {})
        offset = (time.timezone if (time.localtime().tm_isdst == 0)
                  else time.altzone) * -1
        timezone = 'GMT%s%s' % ('+' if offset > 0 else '-',
                                abs(int(offset / 60 / 60)))
        settings = {
            'name': camera_config.get('name', 'unnamed'),
            'timezone': timezone,
            'persists': False,
            }
        msg = make_message('ChangeDeviceSettings',
                           responseExpected=True,
                           payload=settings)
        reply = yield from self.send_to_client(camera_state.websocket, msg)
        self.log.debug('Changed device settings for %s' % camera_state.camera_mac)

    @asyncio.coroutine
    def set_params(self, camera_state):
        payload = {
            'heartbeatsTimeoutMs': 20000,
        }
        msg = make_message('ubnt_avclient_paramAgreement',
                           responseExpected=True,
                           inResponseTo=0,
                           payload=payload)
        yield from self.send_to_client(camera_state.websocket, msg,
                                       really=False)

    @asyncio.coroutine
    def reconfig_streams(self, camera_state):
        self.log.debug('Reconfiguring active streams: %s' % camera_state.streams)
        for stream, (host, port) in camera_state.streams.items():
            yield from self._start_video(camera_state, None, None, stream=stream)

    @asyncio.coroutine
    def do_auth(self, websocket, username, password):
        # Don't know how to actually validate password yet
        authId = uuid.uuid4().hex
        challenge = uuid.uuid4().hex
        msg = make_message('ubnt_avclient_auth',
                           responseExpected=True,
                           payload={
                               'username': username,
                               'stage': 0,
                               'authId': authId,
                               'challenge': challenge,
                               'commonSecret': '',
                               'hashSalt': None,
                               'error': None,
                               'completionCode': 2,
                           })
        self.log.debug('Auth challenge')
        response = yield from self.send_to_client(websocket, msg)
        msg['payload']['commonSecret'] = response['payload']['commonSecret']
        msg['payload']['completionCode'] = 0
        msg['responseExpected'] = False
        self.log.debug('Auth response')
        yield from self.send_to_client(websocket, msg)

    @asyncio.coroutine
    def _start_video(self, camera_state, host, port, stream='video1'):
        vconf = camera_state.conf.get('video', {}).get(stream, {})

        stream_info = {
            "bitRateCbrAvg": vconf.get('bitRateCbrAvg'),
            "bitRateVbrMax": vconf.get('bitRateVbrMax'),
            "bitRateVbrMin": vconf.get('bitRateVbrMin'),
            "fps": vconf.get('fps'),
            "isCbr": str(vconf.get('isCbr', 'False')).lower() == 'true',
            "avSerializer": None
        }

        if host and port:
            stream_info['avSerializer'] = {
                "destinations":[
                    "tcp://%s:%i?retryInterval=1&connectTimeout=30" % (
                        host, port)],
                "type":"flv",
                "streamName":"vMamKDLMVvIxkX9a"
            }
            self.log.info('Starting %s streaming to %s:%i' % (stream, host, port))

        payload = {
            "video": {"fps":  None,
                      "bitrate": None,
                      "video1": None,
                      "video2": None,
                      "video3": None}
        }
        payload['video'][stream] = stream_info

        msg = make_message('ChangeVideoSettings',
                           responseExpected=True,
                           payload=payload)
        response = yield from self.send_to_client(camera_state.websocket, msg)
        # Start sends an extra reply?
        response = yield from self.read_from_client(camera_state.websocket,
                                                    inResponseTo=msg['messageId'])
        camera_state.streams[stream] = (host, port)

    @asyncio.coroutine
    def _stop_video(self, camera_state):
        stream_info = {
            "bitRateCbrAvg": None,
            "bitRateVbrMax": None,
            "bitRateVbrMin": None,
            "fps": None,
            "isCbr": False,
            "avSerializer":{
                "destinations":["file:///dev/null"],
                "type":"flv",
                "streamName":""
            }
        }
        payload = {
            "video": {"fps":  None,
                      "bitrate": None,
                      "video1": stream_info,
                      "video2": None,
                      "video3": None}
        }

        msg = make_message('ChangeVideoSettings',
                           responseExpected=True,
                           payload=payload)
        self.log.debug('Stopping stream')
        response = yield from self.send_to_client(camera_state.websocket, msg)
        # Stop sends an extra reply?
        response = yield from self.read_from_client(camera_state.websocket,
                                                    inResponseTo=msg['messageId'])
        try:
            del camera_state.streams['video1']
        except:
            pass

    def _send_time_sync(self, camera_state, inResponseTo=0):
        timestamp = int(time.time() * 1000)
        msg = make_message('ubnt_avclient_timeSync',
                           payload={'t1': timestamp,
                                    't2': timestamp},
                           inResponseTo=inResponseTo)
        yield from self.send_to_client(camera_state.websocket, msg)
        self.log.debug('Sent time sync to %s for %s' % (
            camera_state.camera_mac,
            time.asctime(time.localtime(timestamp / 1000))))
        camera_state.last_time_sync = time.time()

    @asyncio.coroutine
    def heartbeat(self, camera_state):
        msg = make_message('__av_internal____heartbeat__')
        yield from self.send_to_client(camera_state.websocket, msg)
        self.log.debug('Sent heartbeat')
        if len(camera_state.websocket.msgq) != 0:
            self.log.debug('Message queue: %s - %s' % (len(camera_state.websocket.msgq),
                              ['%s#%s' % (x['functionName'],
                                          x['inResponseTo'])
                               for x in camera_state.websocket.msgq]))

        time_since_sync = time.time() - camera_state.last_time_sync
        if time_since_sync > camera_state.time_sync_interval:
            yield from self._send_time_sync(camera_state)

    def process_status(self, camera_state):
        while True:
            try:
                msg = yield from camera_state.websocket.recv(blocking=False)
            except asyncio.queues.QueueEmpty:
                break
            if msg['functionName'] == '__av_internal____heartbeat__':
                pass
                self.log.debug('Received camera heartbeat')
            elif msg['functionName'] == 'EventStreamingStatus':
                self.log.debug('Streaming status is %s' % msg['payload']['currentStatus'])
            elif msg['functionName'] == 'ubnt_avclient_timeSync':
                delta = msg['payload']['timeDelta']
                self.log.debug('Camera %s reports time delta %i ms' % (
                    camera_state.camera_mac, delta))
                yield from self._send_time_sync(camera_state,
                                                inResponseTo=msg['messageId'])
                if delta != 0 and abs(delta) < 500:
                    camera_state.time_sync_interval = 600
                else:
                    camera_state.time_sync_interval = 120
            else:
                self.log.debug('Received %s message: %s' % (msg['functionName'],
                                                            msg))


if __name__ == '__main__':
    server = UVCWebsocketServer()
    start_server = server.make_server(18443)
    asyncio.get_event_loop().run_until_complete(start_server)
    asyncio.get_event_loop().run_forever()

