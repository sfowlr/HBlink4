"""
Userspace SCTP transport via libusrsctp (ctypes).

Provides SCTP over UDP encapsulation (RFC 6951) on any platform, and plain
SCTP where there's no kernel SCTP (macOS, as root).  Uses the same usrsctp
library that Chrome/Firefox use for WebRTC data channels.

Install: ``brew install libusrsctp`` (macOS) or ``apt install libusrsctp2``
(Linux).  A copy in the virtualenv's ``lib/`` is found too.

With ``sctp_encap: "udp"`` this backend carries the UDP-encapsulated
associations on Linux as well; the kernel's SCTP (sctp.py) still takes plain
SCTP alongside it.
"""

import asyncio
import ctypes
import errno
import ctypes.util
import logging
import os
import platform
import socket
import struct
import sys
import threading
from ctypes import (
    CDLL, CFUNCTYPE, POINTER,
    c_char_p, c_int, c_size_t, c_ssize_t, c_uint8, c_uint16, c_uint32,
    c_void_p, byref, cast, create_string_buffer, sizeof,
)
from typing import Callable, Dict, Optional, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from .hblink import HBProtocol

try:
    from .utils import normalize_addr
except ImportError:
    from utils import normalize_addr

LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Library loading
# ---------------------------------------------------------------------------

def _find_libusrsctp() -> Optional[str]:
    """Locate libusrsctp shared library on the system."""
    # ctypes.util.find_library doesn't always work on macOS with Homebrew
    # due to SIP restrictions.  Try common paths explicitly.
    path = ctypes.util.find_library('usrsctp')
    if path:
        return path

    # A copy in the virtualenv (sys.prefix/lib), then Homebrew paths
    # (Apple Silicon and Intel) and the distributions' library directories.
    candidates = [
        os.path.join(sys.prefix, 'lib', 'libusrsctp.dylib'),
        os.path.join(sys.prefix, 'lib', 'libusrsctp.so.2'),
        os.path.join(sys.prefix, 'lib', 'libusrsctp.so'),
        '/opt/homebrew/lib/libusrsctp.dylib',
        '/usr/local/lib/libusrsctp.dylib',
        '/usr/lib/libusrsctp.so',
        '/usr/lib/x86_64-linux-gnu/libusrsctp.so',
        '/usr/lib/aarch64-linux-gnu/libusrsctp.so',
        '/usr/lib/x86_64-linux-gnu/libusrsctp.so.2',
        '/usr/lib/aarch64-linux-gnu/libusrsctp.so.2',
    ]
    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate
    return None


def _load_libusrsctp() -> Optional[CDLL]:
    """Load libusrsctp and return the CDLL handle, or None if unavailable."""
    path = _find_libusrsctp()
    if path is None:
        return None
    try:
        return CDLL(path, use_errno=True)
    except OSError:
        return None


_lib = _load_libusrsctp()
USRSCTP_AVAILABLE = _lib is not None

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

IPPROTO_SCTP = 132
SCTP_RTOINFO = 0x00000001
SCTP_NODELAY = 0x00000004
SCTP_REMOTE_UDP_ENCAPS_PORT = 0x00000024
SCTP_SENDV_NOINFO = 0
SCTP_SENDV_PRINFO = 2
SCTP_PR_SCTP_TTL = 0x0001
SCTP_EVENT_READ = 0x0001
SCTP_EVENT_WRITE = 0x0002
SCTP_EVENT_ERROR = 0x0004
MSG_NOTIFICATION = 0x2000
# recvv errors that only mean "nothing more to read now"
_RETRY_ERRNOS = (errno.EAGAIN, errno.EWOULDBLOCK, errno.EINTR)
MSG_EOR = 0x8

# sockaddr_conn layout differs between BSD (macOS) and Linux
_IS_BSD = sys.platform in ('darwin', 'freebsd', 'openbsd', 'netbsd')


# ---------------------------------------------------------------------------
# ctypes structure definitions
# ---------------------------------------------------------------------------

class SockaddrIn(ctypes.Structure):
    _fields_ = [
        ('sin_len', c_uint8),        # BSD only, but we always include for alignment
        ('sin_family', c_uint8),
        ('sin_port', c_uint16),
        ('sin_addr', c_uint8 * 4),
        ('sin_zero', c_uint8 * 8),
    ] if _IS_BSD else [
        ('sin_family', c_uint16),
        ('sin_port', c_uint16),
        ('sin_addr', c_uint8 * 4),
        ('sin_zero', c_uint8 * 8),
    ]


class SockaddrIn6(ctypes.Structure):
    _fields_ = [
        ('sin6_len', c_uint8),
        ('sin6_family', c_uint8),
        ('sin6_port', c_uint16),
        ('sin6_flowinfo', c_uint32),
        ('sin6_addr', c_uint8 * 16),
        ('sin6_scope_id', c_uint32),
    ] if _IS_BSD else [
        ('sin6_family', c_uint16),
        ('sin6_port', c_uint16),
        ('sin6_flowinfo', c_uint32),
        ('sin6_addr', c_uint8 * 16),
        ('sin6_scope_id', c_uint32),
    ]


# sctp_rcvinfo struct for usrsctp_recvv
class SctpRcvinfo(ctypes.Structure):
    _fields_ = [
        ('rcv_sid', c_uint16),
        ('rcv_ssn', c_uint16),
        ('rcv_flags', c_uint16),
        ('rcv_ppid', c_uint32),
        ('rcv_tsn', c_uint32),
        ('rcv_cumtsn', c_uint32),
        ('rcv_context', c_uint32),
        ('rcv_assoc_id', c_uint32),
    ]


class SctpRtoinfo(ctypes.Structure):
    """struct sctp_rtoinfo (milliseconds)."""
    _fields_ = [
        ('srto_assoc_id', c_uint32),
        ('srto_initial', c_uint32),
        ('srto_max', c_uint32),
        ('srto_min', c_uint32),
    ]


class SctpUdpencaps(ctypes.Structure):
    """struct sctp_udpencaps: sockaddr_storage (128 bytes, 8-aligned), assoc id, port (network order)."""
    _fields_ = [
        ('sue_address', ctypes.c_uint64 * 16),
        ('sue_assoc_id', c_uint32),
        ('sue_port', c_uint16),
    ]


class SctpPrinfo(ctypes.Structure):
    """struct sctp_prinfo: the PR-SCTP policy and its value (TTL: milliseconds)."""
    _fields_ = [
        ('pr_policy', c_uint16),
        ('pr_value', c_uint32),
    ]


# ---------------------------------------------------------------------------
# Callback types
# ---------------------------------------------------------------------------

# int conn_output(void *addr, void *buffer, size_t length, uint8_t tos, uint8_t set_df)
CONN_OUTPUT_CB = CFUNCTYPE(c_int, c_void_p, c_void_p, c_size_t, c_uint8, c_uint8)

# void debug_printf(const char *format, ...)
DEBUG_PRINTF_CB = CFUNCTYPE(None, c_char_p)

# int receive_cb(struct socket *sock, union sctp_sockstore addr, void *data,
#                size_t datalen, struct sctp_rcvinfo, int flags, void *ulp_info)
# Note: We use upcall mechanism instead of receive callback for flexibility.
UPCALL_CB = CFUNCTYPE(None, c_void_p, c_void_p, c_int)


# ---------------------------------------------------------------------------
# Library initialization
# ---------------------------------------------------------------------------

_initialized = False
_init_lock = threading.Lock()
_encap_port: Optional[int] = None  # the UDP port usrsctp was initialized with


def _debug_printf(fmt):
    """usrsctp debug output handler."""
    try:
        msg = fmt.decode('utf-8', errors='replace').rstrip()
        if msg:
            LOGGER.debug(f'[usrsctp] {msg}')
    except Exception:
        pass


# Keep reference to prevent GC
_debug_cb = DEBUG_PRINTF_CB(_debug_printf)


def _init_usrsctp(udp_encap_port: int = 9899):
    """Initialize the usrsctp library (once per process).

    Args:
        udp_encap_port: UDP encapsulation port.  0 = raw IP (requires root on macOS).
    """
    global _initialized, _encap_port
    if _initialized:
        if udp_encap_port != _encap_port:
            LOGGER.warning(f'usrsctp already initialized with encap_port={_encap_port}; '
                           f'ignoring {udp_encap_port}')
        return
    with _init_lock:
        if _initialized:
            return
        # conn_output=NULL means usrsctp manages its own UDP encapsulation
        _lib.usrsctp_init(c_uint16(udp_encap_port), None, _debug_cb)
        _initialized = True
        _encap_port = udp_encap_port
        LOGGER.info(f'usrsctp initialized (encap_port={udp_encap_port})')


def shutdown_usrsctp():
    """Shutdown the usrsctp library. Call once at process exit."""
    global _initialized, _encap_port
    if not _initialized:
        return
    _lib.usrsctp_finish()
    _initialized = False
    _encap_port = None


# ---------------------------------------------------------------------------
# Helper: build sockaddr from Python (ip, port) tuple
# ---------------------------------------------------------------------------

def _make_sockaddr(ip: str, port: int):
    """Build a ctypes sockaddr_in or sockaddr_in6 from (ip, port)."""
    if ':' in ip:
        addr = SockaddrIn6()
        if _IS_BSD:
            addr.sin6_len = sizeof(SockaddrIn6)
            addr.sin6_family = socket.AF_INET6
        else:
            addr.sin6_family = socket.AF_INET6
        addr.sin6_port = socket.htons(port)
        packed = socket.inet_pton(socket.AF_INET6, ip)
        ctypes.memmove(addr.sin6_addr, packed, 16)
        return addr
    else:
        addr = SockaddrIn()
        if _IS_BSD:
            addr.sin_len = sizeof(SockaddrIn)
            addr.sin_family = socket.AF_INET
        else:
            addr.sin_family = socket.AF_INET
        addr.sin_port = socket.htons(port)
        packed = socket.inet_pton(socket.AF_INET, ip)
        ctypes.memmove(addr.sin_addr, packed, 4)
        return addr


def _extract_addr(sa_ptr, sa_len) -> Tuple[str, int]:
    """Extract (ip, port) from a sockaddr pointer."""
    if sa_len == 0 or sa_ptr is None:
        return ('0.0.0.0', 0)
    # Peek at family byte
    family_offset = 1 if _IS_BSD else 0
    raw = ctypes.string_at(sa_ptr, sa_len)
    if _IS_BSD:
        family = raw[1]
    else:
        family = struct.unpack_from('H', raw, 0)[0]

    if family == socket.AF_INET:
        sa = SockaddrIn.from_buffer_copy(raw[:sizeof(SockaddrIn)])
        port = socket.ntohs(sa.sin_port)
        ip = socket.inet_ntop(socket.AF_INET, bytes(sa.sin_addr))
        return (ip, port)
    elif family == socket.AF_INET6:
        sa = SockaddrIn6.from_buffer_copy(raw[:sizeof(SockaddrIn6)])
        port = socket.ntohs(sa.sin6_port)
        ip = socket.inet_ntop(socket.AF_INET6, bytes(sa.sin6_addr))
        return (ip, port)
    return ('0.0.0.0', 0)


# ---------------------------------------------------------------------------
# Set up function signatures
# ---------------------------------------------------------------------------

if _lib is not None:
    _lib.usrsctp_init.restype = None
    _lib.usrsctp_init.argtypes = [c_uint16, c_void_p, DEBUG_PRINTF_CB]

    _lib.usrsctp_socket.restype = c_void_p
    _lib.usrsctp_socket.argtypes = [c_int, c_int, c_int, c_void_p, c_void_p, c_uint32, c_void_p]

    _lib.usrsctp_bind.restype = c_int
    _lib.usrsctp_bind.argtypes = [c_void_p, c_void_p, ctypes.c_uint32]

    _lib.usrsctp_listen.restype = c_int
    _lib.usrsctp_listen.argtypes = [c_void_p, c_int]

    _lib.usrsctp_accept.restype = c_void_p
    _lib.usrsctp_accept.argtypes = [c_void_p, c_void_p, POINTER(ctypes.c_uint32)]

    _lib.usrsctp_connect.restype = c_int
    _lib.usrsctp_connect.argtypes = [c_void_p, c_void_p, ctypes.c_uint32]

    _lib.usrsctp_sendv.restype = c_ssize_t
    _lib.usrsctp_sendv.argtypes = [
        c_void_p, c_void_p, c_size_t,
        c_void_p, c_int,
        c_void_p, ctypes.c_uint32, c_uint32, c_int
    ]

    _lib.usrsctp_recvv.restype = c_ssize_t
    _lib.usrsctp_recvv.argtypes = [
        c_void_p, c_void_p, c_size_t,
        c_void_p, POINTER(ctypes.c_uint32),
        c_void_p, POINTER(ctypes.c_uint32), POINTER(c_uint32), POINTER(c_int)
    ]

    _lib.usrsctp_close.restype = None
    _lib.usrsctp_close.argtypes = [c_void_p]

    _lib.usrsctp_finish.restype = c_int
    _lib.usrsctp_finish.argtypes = []

    _lib.usrsctp_setsockopt.restype = c_int
    _lib.usrsctp_setsockopt.argtypes = [c_void_p, c_int, c_int, c_void_p, ctypes.c_uint32]

    _lib.usrsctp_getsockopt.restype = c_int
    _lib.usrsctp_getsockopt.argtypes = [c_void_p, c_int, c_int, c_void_p, POINTER(ctypes.c_uint32)]

    _lib.usrsctp_set_non_blocking.restype = c_int
    _lib.usrsctp_set_non_blocking.argtypes = [c_void_p, c_int]

    _lib.usrsctp_set_upcall.restype = c_int
    _lib.usrsctp_set_upcall.argtypes = [c_void_p, UPCALL_CB, c_void_p]

    _lib.usrsctp_get_events.restype = c_int
    _lib.usrsctp_get_events.argtypes = [c_void_p]


# ---------------------------------------------------------------------------
# Socket options
# ---------------------------------------------------------------------------

def set_rto(sock_ptr, rto: Tuple[int, int, int]) -> bool:
    """Set the retransmission timeout bounds (initial, min, max) in ms.

    On a listening socket this sets the defaults its associations get.
    """
    initial, rto_min, rto_max = rto
    info = SctpRtoinfo(0, initial, rto_max, rto_min)
    rc = _lib.usrsctp_setsockopt(sock_ptr, IPPROTO_SCTP, SCTP_RTOINFO, byref(info), sizeof(info))
    if rc < 0:
        LOGGER.warning(f'usrsctp: failed to set SCTP_RTOINFO {rto}')
        return False
    return True


def get_rto(sock_ptr) -> Optional[Tuple[int, int, int]]:
    """The socket's (initial, min, max) RTO in ms, or None."""
    info = SctpRtoinfo()
    size = ctypes.c_uint32(sizeof(info))
    if _lib.usrsctp_getsockopt(sock_ptr, IPPROTO_SCTP, SCTP_RTOINFO, byref(info), byref(size)) < 0:
        return None
    return (info.srto_initial, info.srto_min, info.srto_max)


def set_remote_encap_port(sock_ptr, port: int) -> bool:
    """Send this socket's associations to the peer's UDP port *port*.

    usrsctp learns an inbound peer's UDP port from its packets, but for an
    association we start it must be told, or it sends plain SCTP over IP.
    """
    encaps = SctpUdpencaps()
    encaps.sue_port = socket.htons(port)
    rc = _lib.usrsctp_setsockopt(sock_ptr, IPPROTO_SCTP, SCTP_REMOTE_UDP_ENCAPS_PORT,
                                 byref(encaps), sizeof(encaps))
    if rc < 0:
        LOGGER.warning(f'usrsctp: failed to set the remote UDP encapsulation port {port}')
        return False
    return True


# ---------------------------------------------------------------------------
# UsrsctpSocket: wraps one usrsctp association
# ---------------------------------------------------------------------------

class UsrsctpSocket:
    """Wraps a single usrsctp socket with send/recv/close operations.

    *dmrd_ttl_ms* > 0 sends DMRD (voice) with a PR-SCTP timed lifetime: a
    burst that can't get through within that long is given up rather than
    holding up the ones behind it.  Everything else stays reliable.
    """

    def __init__(self, sock_ptr: c_void_p, peername: Tuple[str, int],
                 loop: asyncio.AbstractEventLoop,
                 on_data: Callable[[bytes, Tuple[str, int]], None],
                 on_close: Callable[['UsrsctpSocket'], None],
                 dmrd_ttl_ms: int = 0):
        self._sock = sock_ptr
        self.peername = peername
        self._loop = loop
        self._on_data = on_data
        self._on_close = on_close
        self._closed = False
        self.dmrd_ttl_ms = dmrd_ttl_ms
        # Built once: the hot path only passes a reference
        self._prinfo = SctpPrinfo(SCTP_PR_SCTP_TTL, dmrd_ttl_ms)
        self._prinfo_ref = byref(self._prinfo)
        self._prinfo_len = ctypes.c_uint32(sizeof(self._prinfo))

        # Set NODELAY
        on = c_int(1)
        _lib.usrsctp_setsockopt(
            self._sock, IPPROTO_SCTP, SCTP_NODELAY, byref(on), sizeof(on)
        )

        # Set non-blocking
        _lib.usrsctp_set_non_blocking(self._sock, 1)

        # Set upcall for read notifications
        self._upcall_ref = UPCALL_CB(self._upcall)
        _lib.usrsctp_set_upcall(self._sock, self._upcall_ref, None)

    def _upcall(self, sock_ptr, arg, flags):
        """Called by usrsctp when socket has events (read/write/error)."""
        if self._closed:
            return
        events = _lib.usrsctp_get_events(sock_ptr)
        if events & (SCTP_EVENT_READ | SCTP_EVENT_ERROR):
            self._drain_recv()

    def _drain_recv(self):
        """Read all available messages from the socket."""
        buf = create_string_buffer(65536)
        from_addr = create_string_buffer(128)
        fromlen = ctypes.c_uint32(128)
        info = SctpRcvinfo()
        infolen = ctypes.c_uint32(sizeof(SctpRcvinfo))
        infotype = c_uint32(0)
        msg_flags = c_int(0)

        while not self._closed:
            fromlen.value = 128
            infolen.value = sizeof(SctpRcvinfo)
            infotype.value = 0
            msg_flags.value = 0

            n = _lib.usrsctp_recvv(
                self._sock, buf, c_size_t(65536),
                from_addr, byref(fromlen),
                byref(info), byref(infolen), byref(infotype), byref(msg_flags)
            )
            if n <= 0:
                # 0: the peer shut down. An error other than "nothing more
                # to read": the association is gone (aborted, or the peer
                # was given up on after retransmissions or heartbeats).
                if n == 0 or ctypes.get_errno() not in _RETRY_ERRNOS:
                    self._loop.call_soon_threadsafe(self._handle_close)
                break

            # Skip notifications
            if msg_flags.value & MSG_NOTIFICATION:
                continue

            data = ctypes.string_at(buf, n)
            self._loop.call_soon_threadsafe(self._on_data, data, self.peername)

    def _handle_close(self):
        """Handle connection close on the asyncio loop."""
        if not self._closed:
            self._closed = True
            _lib.usrsctp_close(self._sock)
            self._on_close(self)

    def send(self, data: bytes) -> None:
        """Send data over this SCTP association. Thread-safe."""
        if self._closed:
            return
        if self.dmrd_ttl_ms and data.startswith(b'DMRD'):
            n = _lib.usrsctp_sendv(
                self._sock,
                data, len(data),
                None, 0,             # no destination (connected socket)
                self._prinfo_ref, self._prinfo_len,
                SCTP_SENDV_PRINFO,
                0
            )
        else:
            n = _lib.usrsctp_sendv(
                self._sock,
                data, len(data),
                None, 0,             # no destination (connected socket)
                None, 0,             # no sndinfo
                SCTP_SENDV_NOINFO,
                0
            )
        if n < 0:
            LOGGER.warning(f'usrsctp_sendv failed for {self.peername}')

    def close(self):
        """Close this SCTP association."""
        if self._closed:
            return
        self._closed = True
        _lib.usrsctp_close(self._sock)


# ---------------------------------------------------------------------------
# UsrsctpInboundProtocol: adapter for inbound connections
# ---------------------------------------------------------------------------

class UsrsctpInboundProtocol:
    """Adapter matching the SCTPInboundProtocol interface for usrsctp associations.

    Manages a single accepted inbound association, delegates packet handling
    to HBProtocol.datagram_received().
    """

    def __init__(self, hbprotocol: 'HBProtocol', usrsctp_sock: UsrsctpSocket):
        self.hbprotocol = hbprotocol
        self.usrsctp_sock = usrsctp_sock
        self.peername = usrsctp_sock.peername
        ip, port = self.peername
        LOGGER.info(f'usrsctp inbound connection from {ip}:{port}')
        # Register in transport dict for _send_packet
        normalized = normalize_addr(self.peername)
        hbprotocol._sctp_transports[normalized] = self

    def write(self, data: bytes):
        """asyncio Transport-compatible write method."""
        self.usrsctp_sock.send(data)

    def get_extra_info(self, key, default=None):
        """Minimal Transport interface for compatibility."""
        if key == 'peername':
            return self.peername
        if key == 'socket':
            return None
        return default

    def connection_lost(self):
        """Called when the usrsctp connection is lost."""
        ip, port = self.peername
        LOGGER.info(f'usrsctp inbound connection lost from {ip}:{port}')
        normalized = normalize_addr(self.peername)
        self.hbprotocol._sctp_transports.pop(normalized, None)
        # Clean up associated repeater
        for rid, rep in list(self.hbprotocol._repeaters.items()):
            if rep.ip == ip and rep.port == port:
                self.hbprotocol._remove_repeater(rid, 'sctp_connection_lost')
                break


# ---------------------------------------------------------------------------
# UsrsctpOutboundProtocol: adapter for outbound connections
# ---------------------------------------------------------------------------

class UsrsctpOutboundProtocol:
    """Adapter matching the SCTPOutboundProtocol interface for usrsctp outbound."""

    def __init__(self, hbprotocol: 'HBProtocol', connection_name: str,
                 usrsctp_sock: UsrsctpSocket):
        self.hbprotocol = hbprotocol
        self.connection_name = connection_name
        self.usrsctp_sock = usrsctp_sock
        LOGGER.info(f'[{connection_name}] usrsctp outbound connection established')

    def write(self, data: bytes):
        """Send data on the outbound association."""
        self.usrsctp_sock.send(data)

    def connection_lost(self):
        """Called when the outbound connection is lost."""
        LOGGER.info(f'[{self.connection_name}] usrsctp outbound connection lost')
        if self.connection_name in self.hbprotocol._outbounds:
            self.hbprotocol._outbounds[self.connection_name].connected = False


# ---------------------------------------------------------------------------
# UsrsctpListener: accepts inbound associations
# ---------------------------------------------------------------------------

class UsrsctpListener:
    """Listens for inbound SCTP associations via usrsctp.

    Runs an accept loop in a background thread and posts new connections
    to the asyncio event loop.
    """

    def __init__(self, hbprotocol: 'HBProtocol', bind_addr: str, port: int,
                 loop: asyncio.AbstractEventLoop, encap_port: int = 9899,
                 rto: Optional[Tuple[int, int, int]] = None, dmrd_ttl_ms: int = 0):
        self.hbprotocol = hbprotocol
        self.bind_addr = bind_addr
        self.port = port
        self.dmrd_ttl_ms = dmrd_ttl_ms
        self._loop = loop
        self._listen_sock = None
        self._accept_thread = None
        self._running = False
        self._connections: Dict[Tuple[str, int], UsrsctpInboundProtocol] = {}

        # Ensure library initialized
        _init_usrsctp(encap_port)

        # Create and bind listen socket
        self._listen_sock = _lib.usrsctp_socket(
            socket.AF_INET6 if ':' in bind_addr else socket.AF_INET,
            socket.SOCK_STREAM,
            IPPROTO_SCTP,
            None, None, c_uint32(0), None
        )
        if not self._listen_sock:
            raise OSError('Failed to create usrsctp listen socket')

        # Set NODELAY
        on = c_int(1)
        _lib.usrsctp_setsockopt(
            self._listen_sock, IPPROTO_SCTP, SCTP_NODELAY, byref(on), sizeof(on)
        )
        # Retransmission timeouts the accepted associations start with
        if rto is not None:
            set_rto(self._listen_sock, rto)

        # Bind
        sa = _make_sockaddr(bind_addr, port)
        rc = _lib.usrsctp_bind(self._listen_sock, byref(sa), sizeof(sa))
        if rc < 0:
            _lib.usrsctp_close(self._listen_sock)
            raise OSError(f'usrsctp_bind failed for {bind_addr}:{port}')

        # Listen
        rc = _lib.usrsctp_listen(self._listen_sock, 5)
        if rc < 0:
            _lib.usrsctp_close(self._listen_sock)
            raise OSError(f'usrsctp_listen failed for {bind_addr}:{port}')

        LOGGER.info(f'usrsctp listening on {bind_addr}:{port}')

    def start(self):
        """Start the accept loop in a background thread."""
        self._running = True
        self._accept_thread = threading.Thread(
            target=self._accept_loop, daemon=True, name='usrsctp-accept'
        )
        self._accept_thread.start()

    def _accept_loop(self):
        """Background thread: accept incoming connections."""
        while self._running:
            peer_addr = create_string_buffer(128)
            peer_len = ctypes.c_uint32(128)

            new_sock = _lib.usrsctp_accept(
                self._listen_sock, peer_addr, byref(peer_len)
            )
            if not new_sock:
                if self._running:
                    # Brief sleep to avoid busy-spin on transient errors
                    import time
                    time.sleep(0.01)
                continue

            peername = _extract_addr(peer_addr, peer_len.value)

            # Create wrapper and post to asyncio loop
            self._loop.call_soon_threadsafe(
                self._handle_new_connection, new_sock, peername
            )

    def _handle_new_connection(self, sock_ptr, peername: Tuple[str, int]):
        """Called on asyncio loop when a new connection is accepted."""
        def on_data(data: bytes, addr: Tuple[str, int]):
            self.hbprotocol.datagram_received(data, addr)

        def on_close(usrsctp_sock: UsrsctpSocket):
            normalized = normalize_addr(usrsctp_sock.peername)
            proto = self._connections.pop(normalized, None)
            if proto:
                proto.connection_lost()

        usrsctp_sock = UsrsctpSocket(sock_ptr, peername, self._loop, on_data, on_close,
                                     dmrd_ttl_ms=self.dmrd_ttl_ms)
        proto = UsrsctpInboundProtocol(self.hbprotocol, usrsctp_sock)
        self._connections[normalize_addr(peername)] = proto

    def stop(self):
        """Stop accepting and close all connections."""
        self._running = False
        if self._listen_sock:
            _lib.usrsctp_close(self._listen_sock)
            self._listen_sock = None
        for proto in list(self._connections.values()):
            proto.usrsctp_sock.close()
        self._connections.clear()


# ---------------------------------------------------------------------------
# Outbound connection helper
# ---------------------------------------------------------------------------

async def usrsctp_connect(hbprotocol: 'HBProtocol', connection_name: str,
                          ip: str, port: int, encap_port: int = 9899,
                          remote_encap_port: Optional[int] = None,
                          rto: Optional[Tuple[int, int, int]] = None,
                          dmrd_ttl_ms: int = 0,
                          timeout: float = 15.0,
                          ) -> Tuple[UsrsctpOutboundProtocol, Callable[[bytes], None]]:
    """Create an outbound usrsctp connection.

    *encap_port* is our UDP port (0: plain SCTP over IP); *remote_encap_port*
    the server's, by default the same.  Gives up after *timeout* seconds.
    Returns (protocol_adapter, send_callable).
    """
    loop = asyncio.get_running_loop()
    _init_usrsctp(encap_port)

    # Create socket
    family = socket.AF_INET6 if ':' in ip else socket.AF_INET
    sock_ptr = _lib.usrsctp_socket(
        family, socket.SOCK_STREAM, IPPROTO_SCTP,
        None, None, c_uint32(0), None
    )
    if not sock_ptr:
        raise OSError(f'Failed to create usrsctp socket for {connection_name}')

    if rto is not None:
        set_rto(sock_ptr, rto)
    if encap_port:
        set_remote_encap_port(sock_ptr, remote_encap_port or encap_port)

    # Connect without blocking: start it, then poll the socket's events until
    # it's writable (up) or has an error, so a peer that never answers can't
    # hold a thread or the reconnect loop.
    sa = _make_sockaddr(ip, port)
    try:
        _lib.usrsctp_set_non_blocking(sock_ptr, 1)
        rc = _lib.usrsctp_connect(sock_ptr, byref(sa), sizeof(sa))
        if rc < 0:
            err = ctypes.get_errno()
            if err != errno.EINPROGRESS:
                raise OSError(err, f'usrsctp_connect failed for {ip}:{port}: {os.strerror(err)}')
        deadline = loop.time() + timeout
        while True:
            events = _lib.usrsctp_get_events(sock_ptr)
            if events & SCTP_EVENT_ERROR:
                raise OSError(f'usrsctp_connect failed for {ip}:{port}')
            if events & SCTP_EVENT_WRITE:
                break
            if loop.time() >= deadline:
                raise TimeoutError(f'usrsctp_connect to {ip}:{port}: no answer in {timeout:g} s')
            await asyncio.sleep(0.02)
    except BaseException:
        _lib.usrsctp_close(sock_ptr)
        raise

    # Wrap in UsrsctpSocket
    def on_data(data: bytes, addr: Tuple[str, int]):
        hbprotocol._handle_outbound_packet(connection_name, data, addr)

    def on_close(usrsctp_sock: UsrsctpSocket):
        proto.connection_lost()

    usrsctp_sock = UsrsctpSocket(sock_ptr, (ip, port), loop, on_data, on_close,
                                 dmrd_ttl_ms=dmrd_ttl_ms)
    proto = UsrsctpOutboundProtocol(hbprotocol, connection_name, usrsctp_sock)

    return proto, usrsctp_sock.send
