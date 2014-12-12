import json
import logging
import ssl
import time
import uuid

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
    def __init__(self, log=None):
        self._cameras = {}
        self._msgq = []
        self.log = log or logging.getLogger('websocket')

    def make_server(self, port, listen='0.0.0.0'):
        context = ssl.SSLContext(ssl.PROTOCOL_SSLv23)
        context.load_cert_chain('/etc/ssl/certs/ssl-cert-snakeoil.pem',
                                '/etc/ssl/private/ssl-cert-snakeoil.key')
        self.log.info('UVC management server started on port %i' % port)
        return websockets.serve(self.handle_camera,
                                listen, port, ssl=context)

    def is_camera_managed(self, camera_mac):
        return camera_mac in self._cameras

    @asyncio.coroutine
    def start_video(self, camera_mac, host, port):
        try:
            client_info, websocket = self._cameras[camera_mac]
        except KeyError:
            raise NoSuchCamera()

        yield from self._start_video(websocket, host, port)

    @asyncio.coroutine
    def stop_video(self, camera_mac):
        try:
            client_info, websocket = self._cameras[camera_mac]
        except KeyError:
            raise NoSuchCamera()

        yield from self._stop_video(websocket)

    @asyncio.coroutine
    def handle_camera(self, websocket, path):
        websocket = WebSocketWrapper(websocket)
        client_info = yield from self.do_hello(websocket)
        self._cameras[client_info['mac']] = (client_info, websocket)

        self.log.info(
            'Camera `%(name)s` now managed: %(mac)s @ %(ip)s' % client_info)

        yield from self.do_auth(websocket, 'ubnt', 'ubnt')
        yield from self._stop_video(websocket)
        while True:
            self.log.debug('Entering loop for camera %s' % client_info['mac'])
            yield from self.heartbeat(websocket)
            yield from self.process_status(websocket)
            yield from asyncio.sleep(5)

    @asyncio.coroutine
    def read_from_client(self, websocket, inResponseTo=None):
        result = yield from websocket.recv(inResponseTo=inResponseTo)
        self.log.debug('C->S: %s' % result)
        return result

    @asyncio.coroutine
    def send_to_client(self, websocket, data):
        self.log.debug('S->C: %s' % data)
        _ = yield from websocket.send(json.dumps(data))
        if data['responseExpected']:
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
    def _start_video(self, websocket, host, port, stream='video1', **params):
        stream_info = {
            "bitRateCbrAvg": None,
            "bitRateVbrMax": None,
            "bitRateVbrMin": None,
            "fps": None,
            "isCbr": False,
            "avSerializer":{
                "destinations":[
                    "tcp://%s:%i?retryInterval=1&connectTimeout=30" % (
                        host, port)],
                "type":"flv",
                "streamName":"vMamKDLMVvIxkX9a"
            }
        }
        stream_info.update(params)
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
        self.log.debug('Starting %s to %s:%i' % (stream, host, port))
        response = yield from self.send_to_client(websocket, msg)
        # Start sends an extra reply?
        response = yield from self.read_from_client(websocket, inResponseTo=msg['messageId'])


    @asyncio.coroutine
    def _stop_video(self, websocket):
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
        response = yield from self.send_to_client(websocket, msg)
        # Stop sends an extra reply?
        response = yield from self.read_from_client(websocket,
                                                    inResponseTo=msg['messageId'])

    @asyncio.coroutine
    def heartbeat(self, websocket):
        msg = make_message('__av_internal____heartbeat__')
        yield from self.send_to_client(websocket, msg)
        self.log.debug('Sent heartbeat')
        if len(websocket.msgq) != 0:
            self.log.debug('Message queue: %s - %s' % (len(websocket.msgq),
                              ['%s#%s' % (x['functionName'],
                                          x['inResponseTo'])
                               for x in websocket.msgq]))

    def process_status(self, websocket):
        while True:
            try:
                msg = yield from websocket.recv(blocking=False)
            except asyncio.queues.QueueEmpty:
                break
            if msg['functionName'] == '__av_internal____heartbeat__':
                pass
                self.log.debug('Received camera heartbeat')
            elif msg['functionName'] == 'EventStreamingStatus':
                self.log.debug('Streaming status is %s' % msg['payload']['currentStatus'])


if __name__ == '__main__':
    server = UVCWebsocketServer()
    start_server = server.make_server(18443)
    asyncio.get_event_loop().run_until_complete(start_server)
    asyncio.get_event_loop().run_forever()

