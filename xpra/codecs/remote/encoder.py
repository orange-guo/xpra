# This file is part of Xpra.
# Copyright (C) 2014 Antoine Martin <antoine@xpra.org>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

import os
import uuid
from queue import Queue, Empty
from typing import Any
from time import monotonic
from collections import deque
from collections.abc import Sequence
from weakref import WeakValueDictionary
from threading import Event

from xpra import __version__
from xpra.os_util import gi_import
from xpra.util.io import is_socket, load_binary_file
from xpra.util.env import envint, osexpand
from xpra.util.objects import typedict, AtomicInteger
from xpra.scripts.main import connect_to
from xpra.scripts.parsing import parse_display_name
from xpra.scripts.config import InitExit, make_defaults_struct
from xpra.net.common import PacketElement, PacketType
from xpra.codecs.constants import VideoSpec, CodecStateException, TransientCodecException
from xpra.codecs.image import ImageWrapper, PlanarFormat
from xpra.log import Logger

log = Logger("encoder", "remote")

ENCODER_SERVER_TIMEOUT = envint("XPRA_ENCODER_SERVER_TIMEOUT", 5)
ENCODER_SERVER_SOCKET_TIMEOUT = envint("XPRA_ENCODER_SERVER_SOCKET_TIMEOUT", 1)
RECONNECT_DELAY = envint("XPRA_RECONNECT_DELAY", 2000)

try:
    from xpra.client.mixins.mmap import MmapClient
    baseclass = MmapClient
except ImportError:
    from xpra.client.base.stub_client_mixin import StubClientMixin
    baseclass = StubClientMixin


def find_encoder_server_uri(sessions_dir: str) -> str:
    session_dir = os.environ.get("XPRA_SESSION_DIR")
    log(f"find_encoder_server_uri({sessions_dir!r}) {session_dir=!r}")
    encoder_sockets = []
    for sdir in os.listdir(sessions_dir):
        if not sdir.isnumeric():
            continue
        path = os.path.join(sessions_dir, sdir)
        encoder_socket = get_encoder_socket(path)
        if encoder_socket:
            encoder_sockets.append(encoder_socket)
    if not encoder_sockets:
        return ""
    if len(encoder_sockets) > 1:
        # sort them by last modified time:
        times = {}
        for spath in encoder_sockets:
            times[spath] = os.path.getmtime(spath)
        # sort by value:
        times = dict(sorted(times.items(), key=lambda item: - item[1]))
        log.warn(f"{times=}")
        encoder_sockets = tuple(times.keys())
    encoder = encoder_sockets[0]
    log(f"find_encoder_server_uri({sessions_dir!r})=%s, from %i encoders found: %s",
        encoder,len(encoder_sockets), encoder_sockets)
    return encoder


def get_encoder_socket(session_dir: str) -> str:
    if not os.path.exists(session_dir) or not os.path.isdir(session_dir):
        return ""
    # there must be a server config:
    config = os.path.join(session_dir, "config")
    if not os.path.exists(config) or not os.path.isfile(config):
        return ""
    # and a socket we can connect to:
    socket = os.path.join(session_dir, "socket")
    if not os.path.exists(socket) or not is_socket(socket):
        return ""
    # verify that the server is an 'encoding' server:
    cdata = load_binary_file(config)
    if not cdata:
        return ""
    for line in cdata.decode("utf8").splitlines():
        if line.startswith("mode="):
            mode = line[len("mode="):]
            if mode == "encoder":
                return socket
    return ""


class EncoderClient(baseclass):

    def __init__(self, options: dict):
        log(f"remote.EncoderClient({options})")
        to = typedict(options)
        self.uri = to.strget("uri", "")
        self.server_timeout = to.intget("timeout", ENCODER_SERVER_TIMEOUT)
        self.server_socket_timeout = to.intget("socket-timeout", ENCODER_SERVER_SOCKET_TIMEOUT)
        self.reconnect_delay = to.intget("reconnect-delay", RECONNECT_DELAY)
        self.connect_timer = 0
        self.encodings: Sequence[str] = ()
        self.specs: dict[str, dict[str, Sequence[VideoSpec]]] = {}
        self.protocol = None
        self._ordinary_packets = []
        self.event = Event()
        self.encoders = WeakValueDictionary()
        opts = make_defaults_struct()
        opts.mmap = "both"
        opts.mmap_group = ""
        super().init(opts)
        self.sessions_dir = osexpand(to.strget("sessions-dir", opts.sessions_dir))

    def __repr__(self):
        return "EncoderClient(%s)" % self.uri

    def schedule_reconnect(self):
        if self.connect_timer:
            return
        GLib = gi_import("GLib")
        self.connect_timer = GLib.timeout_add(self.reconnect_delay, self.reconnect)

    def reconnect(self) -> bool:
        try:
            self.do_connect()
        except (OSError, InitExit):
            log("failed to re-connect", exc_info=True)
            return True
        if not self.protocol:
            # try again:
            return True
        self.connect_timer = 0
        return False

    def connect(self) -> None:
        if self.protocol:
            log("already connected")
            return
        if self.connect_timer:
            log("connect timer is already due")
            return
        self.do_connect()

    def do_connect(self) -> None:
        uri = self.uri or find_encoder_server_uri(self.sessions_dir)
        if not uri:
            log("no encoder server found")
            return
        opts = make_defaults_struct()
        desc = parse_display_name(ValueError, opts, uri)
        if "timeout" not in desc:
            desc["timeout"] = self.server_socket_timeout
        log(f"EncoderClient.connect() server desc={desc!r}")
        conn = connect_to(desc, opts)
        super().setup_connection(conn)
        self.protocol = self.make_protocol(conn)
        self.send_hello()

    def make_protocol(self, conn):
        from xpra.net.packet_encoding import init_all
        init_all()
        from xpra.net.compression import init_all
        init_all()
        from xpra.net.protocol.factory import get_client_protocol_class
        protocol_class = get_client_protocol_class(conn.socktype)
        protocol = protocol_class(conn, self._process_packet, self._next_packet)
        protocol.enable_default_encoder()
        protocol.enable_default_compressor()
        protocol._log_stats = False
        protocol.large_packets += ["encodings", "context-compress", "context-data"]
        # self.add_packet_handler("setting-change", noop)
        # if conn.timeout > 0:
        #    GLib.timeout_add((conn.timeout + EXTRA_TIMEOUT) * 1000, self.verify_connected)
        return protocol

    def is_connected(self) -> bool:
        return bool(self.protocol) and bool(self.encodings)

    def send(self, packet_type: str, *parts: PacketElement) -> None:
        packet = (packet_type, *parts)
        self._ordinary_packets.append(packet)
        self.protocol.source_has_more()

    def _process_packet(self, proto, packet: PacketType) -> None:
        packet_type = packet[0]
        if packet_type in (
                "hello", "encodings", "startup-complete",
                "setting-change",
                "connection-lost", "disconnect",
                "context-response", "context-data",
        ):
            fn = getattr(self, "_process_%s" % packet_type.replace("-", "_"))
            fn(packet)
        else:
            log.warn(f"Warning: received unexpected {packet_type!r} from encoder server connection {proto}")

    def send_hello(self) -> None:
        caps = {
            "version": __version__,
            "client_type": "encode",
            "uuid": uuid.uuid4().hex,
            "windows": False,
            "keyboard": False,
            "wants": ("encodings", "video", ),
            "encoding": {"core": ("rgb32", "rgb24", )},
            "mouse": False,
            "network-state": False,  # tell older server that we don't have "ping"
        }
        caps.update(super().get_caps())
        from xpra.net.packet_encoding import get_packet_encoding_caps
        caps.update(get_packet_encoding_caps(0))
        from xpra.net.compression import get_compression_caps
        caps.update(get_compression_caps(0))
        log(f"sending hello={caps!r}")
        self.send("hello", caps)
        self.event.clear()
        self.protocol.start()
        self.event.wait(self.server_timeout)

    def _process_hello(self, packet: PacketType) -> None:
        caps = packet[1]
        log("got hello: %s", caps)
        if not super().parse_server_capabilities(typedict(caps)):
            raise RuntimeError("failed to parse capabilities")
        version = caps.get("version")
        log.info("connected to encoder server version %s", version)

    def _process_encodings(self, packet: PacketType) -> None:
        log(f"{packet!r}")
        self.specs = typedict(packet[1]).dictget("video") or {}
        self.encodings = tuple(self.specs.keys())
        log("got encodings=%s", self.encodings)
        log("from specs=%s", self.specs)

    def _process_disconnect(self, packet: PacketType) -> None:
        log("disconnected from server %s", self.protocol)
        self.encodings = ()
        self.protocol = None
        super().cleanup()
        self.schedule_reconnect()

    def _process_connection_lost(self, packet: PacketType) -> None:
        log("connection-lost for server %s", self.protocol)
        self.encodings = ()
        self.protocol = None
        super().cleanup()
        self.schedule_reconnect()

    def _process_startup_complete(self, packet: PacketType) -> None:
        log(f"{packet!r}")
        self.event.set()

    def _next_packet(self) -> tuple[Any, bool, bool]:
        return self._ordinary_packets.pop(0), True, bool(self._ordinary_packets)

    def disconnect(self) -> None:
        p = self.protocol
        if p:
            self.protocol = None
            p.close()

    def get_encodings(self) -> Sequence[str]:
        return self.encodings

    def request_context(self, encoder, encoding: str, width: int, height: int, src_format: str, options: dict):
        seq = encoder.sequence
        codec_type = encoder.codec_type
        self.encoders[seq] = encoder
        self.send("context-request", seq, codec_type, encoding, width, height, src_format, options)

    def _process_context_response(self, packet: PacketType):
        seq = packet[1]
        ok = packet[2]
        message = "" if len(packet) < 4 else packet[3]
        info = {} if len(packet) < 5 else packet[4]
        encoder = self.encoders.get(seq)
        log(f"context-response: {seq}={encoder}, {ok=}, {message=!r}, {info=}")
        if not encoder:
            log.error(f"Error: encoder {seq} not found!")
            return
        if ok:
            encoder.ready = True
        else:
            encoder.closed = True

    def compress(self, encoder, image: ImageWrapper, options: typedict) -> tuple[bytes, dict]:
        log("compress%s", (encoder, image, options))
        metadata = {}
        for attr in ("x", "y", "width", "height", "pixel_format", "depth", "rowstride", "bytesperpixel", "planes", "full_range"):
            metadata[attr] = getattr(image, f"get_{attr}")()
        pixels = image.get_pixels()
        mmap_write_area = getattr(self, "mmap_write_area", None)
        if mmap_write_area:
            nplanes = image.get_planes()
            if nplanes == PlanarFormat.PACKED:
                mmap_data = mmap_write_area.write_data(pixels)
                log("sending image via mmap: %s", mmap_data)
            else:
                mmap_data = []
                for plane in range(nplanes):
                    plane_data = mmap_write_area.write_data(pixels[plane])
                    log("sending plane %i via mmap: %s", plane, plane_data)
                    mmap_data.append(plane_data)
            options["chunks"] = tuple(mmap_data)
            pixels = b""
        self.send("context-compress", encoder.sequence, metadata, pixels, options)

    def _process_context_data(self, packet: PacketType):
        seq, bdata, client_options = packet[1:4]
        encoder = self.encoders.get(seq)
        if not encoder:
            log.error(f"Error: encoder {seq} not found!")
            return
        chunks = client_options.pop("chunks", ())
        if not bdata and chunks:
            mmap_read_area = getattr(self, "mmap_read_area", None)
            mmap_data, free = mmap_read_area.mmap_read(*chunks)
            bdata = bytes(mmap_data)
            free()
        log("server replied with %i bytes, client-options=%s", len(bdata), client_options)
        encoder.compressed_data(bdata, client_options)


def get_version() -> Sequence[int]:
    return 0, 1


def get_type() -> str:
    return "remote"


def get_info() -> dict[str, Any]:
    return {"version": get_version()}


server = None
encodings: Sequence[str] = ()


def get_encodings() -> Sequence[str]:
    return encodings


def init_module(options: dict) -> None:
    log(f"remote.encoder.init_module({options})")
    global encodings, server
    try:
        server = EncoderClient(options)
        server.connect()
    except (InitExit, OSError, RuntimeError):
        log("failed to connect to server, no encodings available", exc_info=True)
        encodings = ()
    else:
        encodings = server.get_encodings()


def cleanup_module() -> None:
    log("remote.cleanup_module()")
    server.disconnect()


def get_runtime_factor() -> float:
    return float(server.is_connected())


def make_spec(espec: dict) -> VideoSpec:
    codec_type = espec.pop("codec_type")

    class RemoteEncoder(Encoder):
        def __init__(self):
            super().__init__(codec_type)

    spec = VideoSpec(codec_class=RemoteEncoder, codec_type=f"remote-{codec_type}")
    for k, v in espec.items():
        if not hasattr(spec, k):
            log.warn(f"Warning: unknown video spec attribute {k!r}")
            continue
        setattr(spec, k, v)
    spec.get_runtime_factor = get_runtime_factor
    return spec


def get_specs() -> Sequence[VideoSpec]:
    # the `server.specs` are dictionaries,
    # which we need to convert to real `VideoSpec` objects:
    specs: Sequence[VideoSpec] = []
    for encoding, csc_specs in server.specs.items():
        for csc, especs in csc_specs.items():
            for espec in especs:
                log(f"remote: {encoding} + {csc}: {espec}")
                specs.append(make_spec(espec))
    log(f"remote.get_specs()={specs}")
    return tuple(specs)


sequence = AtomicInteger()


class Encoder:
    __slots__ = (
        "codec_type", "sequence", "encoding",
        "width", "height", "src_format", "dst_formats", "last_frame_times",
        "ready", "closed", "responses",
        "__weakref__",
    )
    """
    This encoder connects to an encoder server and delegates to it
    """
    def __init__(self, codec_type: str):
        self.codec_type = codec_type
        self.sequence = sequence.increase()
        self.encoding = ""
        self.closed = False
        self.width = self.height = 0
        self.src_format = ""
        self.dst_formats: Sequence[str] = ()
        self.last_frame_times: deque[float] = deque(maxlen=200)
        self.ready = False
        self.responses = Queue(maxsize=1)

    def init_context(self, encoding: str, width: int, height: int, src_format: str, options: typedict) -> None:
        self.encoding = encoding
        self.width = width
        self.height = height
        self.src_format = src_format
        self.dst_formats = options.strtupleget("dst-formats")
        self.closed = False
        try:
            server.connect()
        except (OSError, InitExit) as e:
            log("failed to connect to remote encoder server %s", server)
            log(" %s", e)
            raise TransientCodecException(f"failed to connect: {e}") from None
        if not server.is_connected():
            raise TransientCodecException("not connected")
        server.request_context(self, encoding, width, height, src_format, dict(options))

    def is_ready(self) -> bool:
        return self.ready

    def get_info(self) -> dict[str, Any]:
        info = get_info()
        if not self.src_format:
            return info
        info.update({
            "width": self.width,
            "height": self.height,
            "encoding": self.encoding,
            "src_format": self.src_format,
            "dst_formats": self.dst_formats,
        })
        # calculate fps:
        now = monotonic()
        last_time = now
        cut_off = now - 10.0
        f = 0
        for v in tuple(self.last_frame_times):
            if v > cut_off:
                f += 1
                last_time = min(last_time, v)
        if f > 0 and last_time < now:
            info["fps"] = int(0.5 + f / (now - last_time))
        return info

    def __repr__(self):
        if not self.src_format:
            return "remote_encoder(uninitialized)"
        return f"remote_encoder({self.src_format} - {self.width}x{self.height})"

    def is_closed(self) -> bool:
        return self.closed

    def get_encoding(self) -> str:
        return self.encoding

    def get_width(self) -> int:
        return self.width

    def get_height(self) -> int:
        return self.height

    def get_type(self) -> str:
        return "remote"

    def get_src_format(self) -> str:
        return self.src_format

    def clean(self) -> None:
        self.closed = True
        self.width = 0
        self.height = 0
        self.encoding = ""
        self.src_format = ""
        self.dst_formats = ()
        self.last_frame_times = deque()

    def compress_image(self, image: ImageWrapper, options: typedict) -> tuple[bytes, dict]:
        if not server.is_connected():
            raise CodecStateException("not connected to encoder server")
        server.compress(self, image, options)
        try:
            return self.responses.get(timeout=1)
        except Empty:
            log.warn("Warning: remote encoder timeout waiting for server response")
            log.warn(f" for {self.encoding!r} compression of {image}")
            self.closed = not server.is_connected()
            return b"", {}

    def compressed_data(self, bdata, client_options: dict) -> None:
        log(f"received {self.encoding!r} compressed data: {len(bdata)} bytes, {client_options=}")
        self.responses.put((bdata, client_options))
