import asyncio
import aiohttp
from aiohttp import web
import logging
from logging import handlers
import signal
import socket
import time

import unifi_ws_server

class StreamerContext(object):
    pass


class RequestHandler(aiohttp.server.ServerHttpProtocol):
    def __init__(self, **kwargs):
        self._log = kwargs.pop('log')
        super(RequestHandler, self).__init__(**kwargs)

    @asyncio.coroutine
    def handle_request(self, message, payload):
        self._log.debug('GET %s' % message.path)
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

        while (context.streaming
                   and controller.ws_server.is_camera_managed(camera_mac)):
            yield from asyncio.sleep(1)

        self._log.debug('Closing HTTP streaming connection for %s' % camera_mac)
        response.write_eof()
        context.controller.streaming_stopped(context)


class Streamer(asyncio.Protocol):
    def __init__(self):
        super(Streamer, self).__init__()

    @classmethod
    def factory(cls, context):
        def make_thing():
            instance = cls()
            instance._context = context
            instance.log = context.controller.log.getChild('strm')
            return instance
        return make_thing

    def connection_made(self, transport):
        peername = transport.get_extra_info('peername')
        self.log.info('Connection from %s:%i' % peername)
        self.transport = transport
        self.bytes = 0
        self.last_report = 0
        if not self._context.response.is_headers_sent():
            self._context.response.send_headers()

    def _cleanup_everything(self):
        try:
            result = self._context.controller.streaming_stopped(self._context)
        except:
            self.log.exception('While stopping streaming')

        try:
            self.transport.close()
        except:
            pass

        self.log.debug('Total data proxied: %i KB' % (self.bytes / 1024))

    def connection_lost(self, exc):
        self._cleanup_everything()

    def data_received(self, data):
        try:
            self._context.response.write(data)
            self.bytes += len(data)
        except socket.error:
            self.log.debug('Receiver vanished')
            self._cleanup_everything()
        except Exception as e:
            self.log.exception('Unexpected error: %s' % e)
            self._cleanup_everything()

        if (time.time() - self.last_report) > 10:
            self.log.debug('Proxied %i KB' % (self.bytes / 1024))
            self.last_report = time.time()


class NoSuchCamera(Exception):
    pass


class CameraInUse(Exception):
    pass


class UVCController(object):
    def __init__(self, my_ip, baseport=7000):
        self._cameras = {}
        self.my_ip = my_ip
        self.baseport = baseport
        self.log = logging.getLogger('ctrl')
        self.ws_server = unifi_ws_server.UVCWebsocketServer(
            log=self.log.getChild('ws'))

    @asyncio.coroutine
    def init_server(self, loop):
        port = 9999
        srv = yield from loop.create_server(
            lambda: RequestHandler(log=self.log.getChild('http'), debug=True),
            '0.0.0.0', port)
        self.log.info('HTTP stream server started on port %i' % port)
        return srv

    def start(self):
        loop = self.loop = asyncio.get_event_loop()
        loop.add_signal_handler(signal.SIGUSR1,
                                self.ws_server.reload_all_configs)
        ws_server_server = loop.run_until_complete(
            self.ws_server.make_server(7443))
        http_server = loop.run_until_complete(self.init_server(loop))
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
        context.streaming = True
        context.controller = self
        context.camera_mac = camera_mac
        context.response = response
        context.streamer_port = self.get_free_port()
        self.log.debug('Starting stream listener on port %i for camera %s' % (
            context.streamer_port, camera_mac))
        context.streamer = yield from self.loop.create_server(
            Streamer.factory(context), '0.0.0.0', context.streamer_port)
        self._cameras[camera_mac] = context
        yield from self.ws_server.start_video(camera_mac, self.my_ip,
                                              context.streamer_port)
        return context

    def streaming_stopped(self, context):
        if not context.streaming:
            # We've already done cleanup here
            return

        context.streaming = False

        self.log.info('Stopping %s camera streaming' % context.camera_mac)

        try:
            context.streamer.close()
        except:
            self.log.exception('Failed to stop streaming server')

        @asyncio.coroutine
        def stop():
            try:
                yield from self.ws_server.stop_video(context.camera_mac)
            except unifi_ws_server.NoSuchCamera:
                pass

        asyncio.async(stop())

        del self._cameras[context.camera_mac]


if __name__ == '__main__':
    import sys

    if len(sys.argv) < 2:
        print('You must specify the IP of this server')
        sys.exit(1)

    log_format = '%(asctime)s %(name)s/%(levelname)s: %(message)s'
    date_format = '%Y-%m-%dT%H:%M:%S'

    logging.getLogger(None).setLevel(logging.DEBUG)
    logging.getLogger('asyncio').setLevel(logging.ERROR)
    logging.getLogger('websockets').setLevel(logging.WARNING)

    lf = logging.Formatter(log_format, datefmt=date_format)

    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(lf)
    logging.getLogger(None).addHandler(console)

    debuglg = handlers.RotatingFileHandler('debug.log',
                                           maxBytes=5*1024*1024,
                                           backupCount=4)
    debuglg.setLevel(logging.DEBUG)
    debuglg.setFormatter(lf)
    logging.getLogger(None).addHandler(debuglg)

    controller = UVCController(sys.argv[1])
    controller.start()
