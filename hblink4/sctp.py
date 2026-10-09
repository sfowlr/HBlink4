"""
SCTP transport support for HBlink4.

Provides optional SCTP (Stream Control Transmission Protocol) as an
alternative to UDP for HomeBrew protocol connections.  SCTP preserves
message boundaries (like UDP) while being connection-oriented (like TCP)
with built-in heartbeat detection.

Two backends:

- the Linux kernel's SCTP (``modprobe sctp``): plain SCTP over IP;
- libusrsctp (``usrsctp_transport.py``): SCTP over UDP (RFC 6951) on
  ``sctp_encap_port``, on any platform, or plain SCTP (needs root).

``global.sctp_encap`` picks them: ``"udp"`` (default) listens with usrsctp
over UDP and, where the kernel has SCTP, with the kernel for plain SCTP as
well; ``"raw"`` is plain SCTP only (the kernel, else usrsctp raw).
"""

import asyncio
import logging
import socket
import struct
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from .hblink import HBProtocol

try:
    from .utils import normalize_addr
except ImportError:
    from utils import normalize_addr

LOGGER = logging.getLogger(__name__)

# SCTP protocol number (not always exposed in socket module)
IPPROTO_SCTP = 132

# SCTP socket option constants — not in Python's socket module but stable
# across Linux kernels (include/uapi/linux/sctp.h).
SCTP_RTOINFO = 0          # struct sctp_rtoinfo — retransmission timeout bounds
SCTP_NODELAY = 3          # Disable Nagle — critical for low-latency DMR packets
SCTP_PEER_ADDR_PARAMS = 9 # struct sctp_paddrparams — heartbeat interval tuning

# Probe both backends at import time
KERNEL_SCTP_AVAILABLE = False
try:
    _s = socket.socket(socket.AF_INET, socket.SOCK_STREAM, IPPROTO_SCTP)
    _s.close()
    KERNEL_SCTP_AVAILABLE = True
except (OSError, socket.error):
    pass

USRSCTP_AVAILABLE = False
try:
    from .usrsctp_transport import USRSCTP_AVAILABLE
except ImportError:
    try:
        from usrsctp_transport import USRSCTP_AVAILABLE
    except ImportError:
        pass

SCTP_AVAILABLE = KERNEL_SCTP_AVAILABLE or USRSCTP_AVAILABLE
# The backend for plain SCTP: 'kernel', 'usrsctp' or None
SCTP_BACKEND = 'kernel' if KERNEL_SCTP_AVAILABLE else ('usrsctp' if USRSCTP_AVAILABLE else None)


def check_sctp_available() -> bool:
    """Return True if any SCTP backend (kernel or usrsctp) is available."""
    return SCTP_AVAILABLE


# ---------------------------------------------------------------------------
# Settings (global.sctp_*)
# ---------------------------------------------------------------------------

SCTP_ENCAP_MODES = ('udp', 'raw')
DEFAULT_ENCAP_PORT = 9899
# RFC 9260 starts at 1 s and never goes below 1 s; DMR over Wi-Fi wants
# resends sooner, and a few seconds at most between them during an outage.
DEFAULT_RTO_INITIAL_MS = 1000
DEFAULT_RTO_MIN_MS = 200
DEFAULT_RTO_MAX_MS = 5000


@dataclass(frozen=True)
class SCTPSettings:
    """SCTP settings from ``global``, checked."""
    encap: str = 'udp'                 # 'udp' (RFC 6951) or 'raw'
    encap_port: int = DEFAULT_ENCAP_PORT
    rto_initial_ms: int = DEFAULT_RTO_INITIAL_MS
    rto_min_ms: int = DEFAULT_RTO_MIN_MS
    rto_max_ms: int = DEFAULT_RTO_MAX_MS
    ttl_ms: int = 0                    # PR-SCTP lifetime of our DMRD; 0: reliable

    @property
    def rto(self) -> Tuple[int, int, int]:
        """(initial, min, max) in milliseconds."""
        return (self.rto_initial_ms, self.rto_min_ms, self.rto_max_ms)

    @property
    def usrsctp_encap_port(self) -> int:
        """usrsctp's UDP port: 0 (raw IP) unless encap is 'udp'."""
        return self.encap_port if self.encap == 'udp' else 0


def _int_setting(g: Dict[str, Any], key: str, default: int, lo: int, hi: int) -> int:
    value = g.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        LOGGER.warning(f'⚠️  global.{key} = {value!r} is not a whole number from {lo} to {hi}; '
                       f'using {default}')
        return default
    return value


def sctp_settings(g: Dict[str, Any]) -> SCTPSettings:
    """Read the ``global`` SCTP settings, falling back to defaults (with a
    warning) for any that are out of range."""
    encap = str(g.get('sctp_encap', 'udp')).lower()
    if encap not in SCTP_ENCAP_MODES:
        LOGGER.warning(f'⚠️  global.sctp_encap = {encap!r} is not "udp" or "raw"; using "udp"')
        encap = 'udp'
    encap_port = _int_setting(g, 'sctp_encap_port', DEFAULT_ENCAP_PORT, 1, 65535)
    rto = (_int_setting(g, 'sctp_rto_initial_ms', DEFAULT_RTO_INITIAL_MS, 10, 600000),
           _int_setting(g, 'sctp_rto_min_ms', DEFAULT_RTO_MIN_MS, 10, 600000),
           _int_setting(g, 'sctp_rto_max_ms', DEFAULT_RTO_MAX_MS, 10, 600000))
    if not rto[1] <= rto[0] <= rto[2]:
        LOGGER.warning(f'⚠️  SCTP RTO needs sctp_rto_min_ms <= sctp_rto_initial_ms <= sctp_rto_max_ms '
                       f'(got min {rto[1]}, initial {rto[0]}, max {rto[2]}); using '
                       f'{DEFAULT_RTO_MIN_MS}, {DEFAULT_RTO_INITIAL_MS}, {DEFAULT_RTO_MAX_MS}')
        rto = (DEFAULT_RTO_INITIAL_MS, DEFAULT_RTO_MIN_MS, DEFAULT_RTO_MAX_MS)
    ttl = _int_setting(g, 'sctp_ttl_ms', 0, 0, 600000)
    return SCTPSettings(encap, encap_port, rto[0], rto[1], rto[2], ttl)


def plan_sctp_listeners(encap: str, kernel_ok: bool = None,
                        usrsctp_ok: bool = None) -> Tuple[bool, bool]:
    """Which listeners to start: (kernel, usrsctp).

    ``udp``: usrsctp over UDP when libusrsctp is there, and the kernel's plain
    SCTP alongside it when the kernel has SCTP — the two don't share a port
    (usrsctp only opens its UDP port when not root).  ``raw``: plain SCTP from
    the kernel, else from usrsctp (raw IP, root only).
    """
    kernel_ok = KERNEL_SCTP_AVAILABLE if kernel_ok is None else kernel_ok
    usrsctp_ok = USRSCTP_AVAILABLE if usrsctp_ok is None else usrsctp_ok
    if encap == 'udp':
        return kernel_ok, usrsctp_ok
    return kernel_ok, (usrsctp_ok and not kernel_ok)


def plan_sctp_outbound(encap: str, kernel_ok: bool = None,
                       usrsctp_ok: bool = None) -> Optional[str]:
    """The backend for an outbound association: 'kernel', 'usrsctp' or None.

    ``udp`` needs usrsctp (the kernel's plain SCTP if it's missing); ``raw``
    prefers the kernel.
    """
    kernel_ok = KERNEL_SCTP_AVAILABLE if kernel_ok is None else kernel_ok
    usrsctp_ok = USRSCTP_AVAILABLE if usrsctp_ok is None else usrsctp_ok
    order = ('usrsctp', 'kernel') if encap == 'udp' else ('kernel', 'usrsctp')
    for backend in order:
        if (kernel_ok if backend == 'kernel' else usrsctp_ok):
            return backend
    return None


def _apply_sctp_options(sock: socket.socket) -> None:
    """Apply SCTP-specific socket options for DMR operation.

    Always enables SCTP_NODELAY (disable Nagle) — HomeBrew protocol packets
    are small (53 bytes for DMRD) and latency-sensitive; buffering them would
    add unacceptable delay to the hot path.
    """
    try:
        sock.setsockopt(IPPROTO_SCTP, SCTP_NODELAY, 1)
    except OSError as e:
        LOGGER.warning(f'Failed to set SCTP_NODELAY: {e}')


def apply_sctp_rto(sock: socket.socket, rto: Tuple[int, int, int]) -> None:
    """Set the retransmission timeout bounds (initial, min, max) in ms.

    On a listening socket this sets the defaults its associations get.
    struct sctp_rtoinfo: assoc_id (0: the socket's defaults), initial, max, min.
    """
    initial, rto_min, rto_max = rto
    try:
        sock.setsockopt(IPPROTO_SCTP, SCTP_RTOINFO,
                        struct.pack('=iIII', 0, initial, rto_max, rto_min))
    except OSError as e:
        LOGGER.warning(f'Failed to set SCTP_RTOINFO: {e}')


def create_sctp_listen_socket(bind_addr: str, port: int,
                              rto: Optional[Tuple[int, int, int]] = None) -> socket.socket:
    """Create a non-blocking SCTP listening socket bound to *bind_addr*:*port*.

    The caller is responsible for passing this to ``loop.create_server()``.
    """
    family = socket.AF_INET6 if ':' in bind_addr else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM, IPPROTO_SCTP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if family == socket.AF_INET6:
        # Prevent IPv6 socket from accepting IPv4 connections on Linux
        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
    _apply_sctp_options(sock)
    if rto is not None:
        apply_sctp_rto(sock, rto)
    sock.bind((bind_addr, port))
    sock.setblocking(False)
    return sock


def create_sctp_connect_socket(host: str,
                               rto: Optional[Tuple[int, int, int]] = None) -> socket.socket:
    """Create a non-blocking SCTP client socket for outbound connections.

    The socket is *not* connected — ``loop.create_connection()`` handles that.
    """
    family = socket.AF_INET6 if ':' in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM, IPPROTO_SCTP)
    _apply_sctp_options(sock)
    if rto is not None:
        apply_sctp_rto(sock, rto)
    sock.setblocking(False)
    return sock


# ---------------------------------------------------------------------------
# asyncio Protocol classes
# ---------------------------------------------------------------------------

class SCTPInboundProtocol(asyncio.Protocol):
    """Per-connection protocol for an inbound SCTP association from a repeater.

    Delegates all packet handling to the shared ``HBProtocol`` instance so
    that no handler logic is duplicated.  SCTP preserves message boundaries,
    so each ``data_received`` callback delivers exactly one HomeBrew protocol
    packet — matching UDP semantics.
    """

    def __init__(self, hbprotocol: 'HBProtocol'):
        self.hbprotocol = hbprotocol
        self.transport: Optional[asyncio.Transport] = None
        self.peername: Optional[tuple] = None

    def connection_made(self, transport: asyncio.Transport):
        self.transport = transport
        self.peername = transport.get_extra_info('peername')
        ip, port = self.peername[0], self.peername[1]
        LOGGER.info(f'SCTP connection accepted from {ip}:{port}')
        # Ensure NODELAY on the accepted socket (not always inherited from listen socket)
        sock = transport.get_extra_info('socket')
        if sock is not None and hasattr(sock, 'setsockopt'):
            _apply_sctp_options(sock)
        # Register so _send_packet can reach this peer before a RepeaterState exists
        self.hbprotocol._sctp_transports[normalize_addr(self.peername)] = transport

    def data_received(self, data: bytes):
        if self.peername:
            self.hbprotocol.datagram_received(data, self.peername)

    def connection_lost(self, exc):
        if not self.peername:
            return
        ip, port = self.peername[0], self.peername[1]
        LOGGER.info(f'SCTP connection lost from {ip}:{port}{f": {exc}" if exc else ""}')
        self.hbprotocol._sctp_transports.pop(normalize_addr(self.peername), None)
        # Find and clean up the repeater associated with this connection
        for rid, rep in list(self.hbprotocol._repeaters.items()):
            if rep.ip == ip and rep.port == port:
                self.hbprotocol._remove_repeater(rid, 'sctp_connection_lost')
                break


class SCTPOutboundProtocol(asyncio.Protocol):
    """Per-connection protocol for an outbound SCTP association to a remote server.

    Mirrors ``OutboundProtocol`` (the UDP variant) — receives packets from
    the remote server and dispatches them via ``HBProtocol._handle_outbound_packet``.
    """

    def __init__(self, hbprotocol: 'HBProtocol', connection_name: str):
        self.hbprotocol = hbprotocol
        self.connection_name = connection_name
        self.transport: Optional[asyncio.Transport] = None

    def connection_made(self, transport: asyncio.Transport):
        self.transport = transport
        LOGGER.info(f'[{self.connection_name}] SCTP outbound connection established')

    def data_received(self, data: bytes):
        peername = self.transport.get_extra_info('peername') if self.transport else ('0.0.0.0', 0)
        self.hbprotocol._handle_outbound_packet(self.connection_name, data, peername)

    def connection_lost(self, exc):
        LOGGER.info(f'[{self.connection_name}] SCTP outbound connection lost'
                     f'{f": {exc}" if exc else ""}')
        if self.connection_name in self.hbprotocol._outbounds:
            self.hbprotocol._outbounds[self.connection_name].connected = False
