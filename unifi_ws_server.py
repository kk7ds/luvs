import json
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


def read_from_client(websocket):
    data = yield from websocket.recv()
    return json.loads(data.decode('utf-8'))


def send_to_client(websocket, data):
    yield from websocket.send(json.dumps(data))


def do_hello(websocket):
    client_info = yield from read_from_client(websocket)
    msg = make_message('ubnt_avclient_hello',
                       payload={'protocolVersion': 25,
                                'controllerName': 'unifi_video'},
                       inResponseTo=client_info['messageId'])
    yield from send_to_client(websocket, msg)
    return client_info['payload']


def do_auth(websocket, username, password):
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
    yield from send_to_client(websocket, msg)
    client_msg = yield from read_from_client(websocket)
    msg['payload']['commonSecret'] = client_msg['payload']['commonSecret']
    msg['payload']['completionCode'] = 0
    msg['responseExpected'] = False
    yield from send_to_client(websocket, msg)


def start_video(websocket, host, port, stream='video1', **params):
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
    yield from send_to_client(websocket, msg)
    client_msg = yield from read_from_client(websocket)


def heartbeat(websocket):
    msg = make_message('__av_internal____heartbeat__')
    yield from send_to_client(websocket, msg)


@asyncio.coroutine
def uvc_start_video(websocket, path):
    yield from do_hello(websocket)
    yield from do_auth(websocket, 'ubnt', 'ubnt')
    yield from start_video(websocket, '192.168.201.1', 6667)
    while True:
        yield from heartbeat(websocket)
        time.sleep(5)


if __name__ == '__main__':
    start_server = websockets.serve(uvc_start_video, '0.0.0.0', 18443)
    asyncio.get_event_loop().run_until_complete(start_server)
    asyncio.get_event_loop().run_forever()

