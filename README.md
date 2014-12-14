LUVS - Lightweight Unifi Video Server
=====================================

This is a simple package that provides very primitive management of
Ubiquiti Unifi video cameras. It allows viewing of the MPEG4 stream
with a standard client capable of HTTP streaming (such as
zoneminder). I wrote this to scratch my own itch, tested with gen2
camera firmware version 3.0.9.25 (protocol version 25).

It is still very raw at the moment, probably requiring anyone else to
modify the source to actually use it.

Requirements
------------

* Python >= 3.3
* asyncio
* aiohttp
* websockets
* pyaml

Quickstart
----------

Since I run this on an Ubuntu 12.04 machine, I have to do some extra
work to get the newer requirements. On newer boxes you probably don't
need the python3 PPA.

    $ sudo apt-add-repository ppa:fkrull/deadsnakes
    $ sudo apt-get update
    $ sudo apt-get install python3.3 pip
    $ sudo pip install virtualenv 
 
Then grab the source:

    $ git clone http://github.com/kk7ds/luvs
    $ cd luvs
    $ virtualenv --python=/python3.3 venv
    $ source venv/bin/activate
    $ pip install asyncio websockets aiohttp pyaml

Now you should be able to start the management server and tell it what
the IP address of the box it's running is:

    $ python unifi_stream_server.py 4.5.6.7

The IP is required because that is what it tells the camera to stream
to. If the IP you provide is not accessible to the camera, video will
not be available.

The code currently assumes the Ubuntu-provided SSL keys in /etc/ssl,
so if you're not root and can't read those, the above will fail. You
can run it as root (with sudo) or make these files available to your
user:

    /etc/ssl/certs/ssl-cert-snakeoil.pem
    /etc/ssl/private/ssl-cert-snakeoil.key

When it starts, you should see this:

    2014-12-12T12:42:25 ctrl.ws/INFO: UVC management server started on port 7443
    2014-12-12T12:42:25 ctrl/INFO: HTTP stream server started on port 9999

Now, you need to convince your cameras to connect to this
machine. Assuming your camera's IP is 1.2.3.4 and your host's IP is
4.5.6.7, use the script provided to do this in another terminal:

    $ ./tools/bootstrap_camera.sh ubnt@1.2.3.4 4.5.6.7
    The authenticity of host '1.2.3.4 (1.2.3.4)' can't be established.
    RSA key fingerprint is xx:xx:xx:xx:xx:xx:xx:xx:xx:xx:xx:xx:xx:xx:xx:xx.
    Are you sure you want to continue connecting (yes/no)? yes
    Warning: Permanently added '1.2.3.4' (RSA) to the list of known hosts.
    ubnt@1.2.3.4's password:
    SUCCESS

Alternately, you can do this through the web interface on the camera.

After a few seconds, you should see the camera check in with the
management server:

    2014-12-12T12:42:30 ctrl.ws/INFO: Camera `UVC` now managed: 445566001122 @ 1.2.3.4

At this point, you should be able to hit your management host on port
9999 to get the stream. If you have vlc:

    vlc http://4.5.6.7:9999/445566001122

Caveats
-------

This works for me. That's about all I can say about its fitness.

This is not related to or endorsed in any way to Ubiquiti.

There is no warranty. It may destroy your camera and send
uncomfortable pictures to your facebook friends.

I have done very little to make this consumable by others. I will do
more if people are interested, and won't if not.
