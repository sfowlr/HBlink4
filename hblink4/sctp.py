"""
SCTP transport support for HBlink4.

Provides optional SCTP (Stream Control Transmission Protocol) as an
alternative to UDP for HomeBrew protocol connections.  SCTP preserves
message boundaries (like UDP) while being connection-oriented (like TCP)
with built-in heartbeat detection.

Requires Linux kernel SCTP support (``modprobe sctp``).  macOS does not
have kernel SCTP — the server falls back to UDP-only gracefully.
"""

import asyncio
import logging
import socket
from typing import Optional, TYPE_CHECKING

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
SCTP_NODELAY = 3          # Disable Nagle — critical for low-latency DMR packets
SCTP_PEER_ADDR_PARAMS = 9 # struct sctp_paddrparams — heartbeat interval tuning

# Probe kernel support at import time
SCTP_AVAILABLE = False
try:
    _s = socket.socket(socket.AF_INET, socket.SOCK_STREAM, IPPROTO_SCTP)
    _s.close()
    SCTP_AVAILABLE = True
except (OSError, socket.error):
    pass


def check_sctp_available() -> bool:
    """Return True if the running kernel supports SCTP."""
    return SCTP_AVAILABLE


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


def create_sctp_listen_socket(bind_addr: str, port: int) -> socket.socket:
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
    sock.bind((bind_addr, port))
    sock.setblocking(False)
    return sock


def create_sctp_connect_socket(host: str) -> socket.socket:
    """Create a non-blocking SCTP client socket for outbound connections.

    The socket is *not* connected — ``loop.create_connection()`` handles that.
    """
    family = socket.AF_INET6 if ':' in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM, IPPROTO_SCTP)
    _apply_sctp_options(sock)
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
