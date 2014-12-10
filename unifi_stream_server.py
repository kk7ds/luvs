import asyncio
import aiohttp
from aiohttp import web
import socket
import time

import unifi_ws_server

class StreamerContext(object):
    pass


class RequestHandler(aiohttp.server.ServerHttpProtocol):
    @asyncio.coroutine
    def handle_request(self, message, payload):
        print('Request for %s' % message.path)
        camera_mac = message.path[1:]

        response = aiohttp.Response(self.writer, 200,
                                    http_version=message.version)
        try:
            context = yield from controller.stream_camera(camera_mac, response)
        except NoSuchCamera:
            response = aiohttp.Response(self.writer, 404)
            response.send_headers()
            response.write_eof()
            return
        except CameraInUse:
            response = aiohttp.Response(self.writer, 409)
            response.send_headers()
            response.write_eof()
            return

        response.send_headers()
        yield from context.streamer.wait_closed()


@asyncio.coroutine
def init(loop):
    srv = yield from loop.create_server(
        lambda: RequestHandler(debug=True),
        '0.0.0.0', 9999)
    print('Server started')
    return srv


class Streamer(asyncio.Protocol):
    @classmethod
    def factory(cls, context):
        def make_thing():
            instance = cls()
            instance._context = context
            return instance
        return make_thing

    def connection_made(self, transport):
        print('Connection from ' + str(transport.get_extra_info('peername')))
        self.transport = transport
        self.bytes = 0
        self.last_report = 0

    def data_received(self, data):
        try:
            self._context.response.write(data)
            self._context.response.transport.drain()
            self.bytes += len(data)
        except socket.error:
            print('Receiver vanished')
            self.transport.close()
            self._context.controller.streaming_stopped(self._context)
        except Exception as e:
            print('ERROR WAS %s' % e)
            self.transport.close()
            self._context.controller.streaming_stopped(self._context)

        if (time.time() - self.last_report) > 10:
            print('Proxied %i KB' % (self.bytes / 1024))
            self.last_report = time.time()


class NoSuchCamera(Exception):
    pass


class CameraInUse(Exception):
    pass


class UVCController(object):
    def __init__(self, baseport=7000):
        self._cameras = {}
        self.baseport = baseport
        self.ws_server = unifi_ws_server.UVCWebsocketServer()

    def start(self):
        loop = self.loop = asyncio.get_event_loop()
        ws_server_server = loop.run_until_complete(
            self.ws_server.make_server(18443))
        http_server = loop.run_until_complete(init(loop))
        loop.run_forever()

    def get_free_port(self):
        ports_in_use = [x.streamer_port for x in self._cameras.values()]
        for i in range(self.baseport, self.baseport + 100):
            if i not in ports_in_use:
                return i
        raise Exception('Too many ports')

    def stream_camera(self, camera_mac, response):
        if not self.ws_server.is_camera_managed(camera_mac):
            raise NoSuchCamera('No such camera')

        if camera_mac in self._cameras:
            raise CameraInUse('Camera in use')

        context = StreamerContext()
        context.controller = self
        context.camera_mac = camera_mac
        context.response = response
        context.streamer_port = self.get_free_port()
        print('Starting streamer on port %i' % context.streamer_port)
        context.streamer = yield from self.loop.create_server(
            Streamer.factory(context), '0.0.0.0', context.streamer_port)
        self._cameras[camera_mac] = context
        print('Starting %s camera streaming to port %i' % (camera_mac,
                                                           context.streamer_port))
        yield from self.ws_server.start_video(camera_mac, '192.168.201.1',
                                              context.streamer_port)
        return context

    def streaming_stopped(self, context):
        context.streamer.close()
        print('Stopping %s camera streaming' % context.camera_mac)
        asyncio.async(self.ws_server.stop_video(context.camera_mac))
        del self._cameras[context.camera_mac]


controller = UVCController()
controller.start()
