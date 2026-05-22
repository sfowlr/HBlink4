"""
Tests for usrsctp (userspace SCTP) transport support.

Mock-based tests run on all platforms (no libusrsctp needed).
Integration tests require libusrsctp installed and are skipped otherwise.
"""
import sys
import unittest
from unittest.mock import Mock, patch, MagicMock

from hblink4.usrsctp_transport import (
    USRSCTP_AVAILABLE,
    _find_libusrsctp,
    _make_sockaddr,
    _IS_BSD,
    SockaddrIn,
    SockaddrIn6,
    UsrsctpSocket,
    UsrsctpInboundProtocol,
    UsrsctpOutboundProtocol,
)
from hblink4.sctp import SCTP_AVAILABLE, SCTP_BACKEND
from hblink4.utils import normalize_addr

import ctypes
import socket
import struct


class TestUsrsctpAvailability(unittest.TestCase):
    """Verify usrsctp availability detection."""

    def test_usrsctp_available_is_bool(self):
        self.assertIsInstance(USRSCTP_AVAILABLE, bool)

    @unittest.skipUnless(sys.platform == 'darwin', 'macOS-specific')
    def test_sctp_backend_on_macos(self):
        """On macOS with libusrsctp installed, backend should be 'usrsctp'."""
        if USRSCTP_AVAILABLE:
            self.assertEqual(SCTP_BACKEND, 'usrsctp')
            self.assertTrue(SCTP_AVAILABLE)
        else:
            # libusrsctp not installed — SCTP unavailable
            self.assertFalse(SCTP_AVAILABLE)

    @unittest.skipUnless(sys.platform == 'linux', 'Linux-specific')
    def test_kernel_preferred_on_linux(self):
        """On Linux with kernel SCTP, kernel backend is preferred."""
        from hblink4.sctp import SCTP_AVAILABLE as avail, SCTP_BACKEND as backend
        if avail and backend == 'kernel':
            # Kernel is preferred over usrsctp
            self.assertEqual(backend, 'kernel')


class TestSockaddrConstruction(unittest.TestCase):
    """Test sockaddr struct building."""

    def test_ipv4_sockaddr(self):
        sa = _make_sockaddr('192.168.1.100', 62031)
        self.assertIsInstance(sa, SockaddrIn)
        # Port should be in network byte order
        self.assertEqual(socket.ntohs(sa.sin_port), 62031)
        # Address bytes
        expected = socket.inet_pton(socket.AF_INET, '192.168.1.100')
        self.assertEqual(bytes(sa.sin_addr), expected)

    def test_ipv6_sockaddr(self):
        sa = _make_sockaddr('::1', 62031)
        self.assertIsInstance(sa, SockaddrIn6)
        self.assertEqual(socket.ntohs(sa.sin6_port), 62031)
        expected = socket.inet_pton(socket.AF_INET6, '::1')
        self.assertEqual(bytes(sa.sin6_addr), expected)

    @unittest.skipUnless(sys.platform == 'darwin', 'BSD sockaddr layout')
    def test_bsd_sockaddr_has_len(self):
        sa = _make_sockaddr('10.0.0.1', 80)
        self.assertEqual(sa.sin_len, ctypes.sizeof(SockaddrIn))
        self.assertEqual(sa.sin_family, socket.AF_INET)


class TestUsrsctpInboundProtocol(unittest.TestCase):
    """Test UsrsctpInboundProtocol interface compliance."""

    def _make_mock_protocol(self):
        mock_hb = Mock()
        mock_hb._sctp_transports = {}
        mock_hb._repeaters = {}
        mock_sock = Mock(spec=UsrsctpSocket)
        mock_sock.peername = ('192.168.1.50', 62031)
        mock_sock.send = Mock()
        return mock_hb, mock_sock

    def test_registers_in_sctp_transports(self):
        hb, sock = self._make_mock_protocol()
        proto = UsrsctpInboundProtocol(hb, sock)
        self.assertIn(('192.168.1.50', 62031), hb._sctp_transports)
        self.assertIs(hb._sctp_transports[('192.168.1.50', 62031)], proto)

    def test_write_calls_usrsctp_send(self):
        hb, sock = self._make_mock_protocol()
        proto = UsrsctpInboundProtocol(hb, sock)
        proto.write(b'DMRD' + b'\x00' * 49)
        sock.send.assert_called_once_with(b'DMRD' + b'\x00' * 49)

    def test_get_extra_info_peername(self):
        hb, sock = self._make_mock_protocol()
        proto = UsrsctpInboundProtocol(hb, sock)
        self.assertEqual(proto.get_extra_info('peername'), ('192.168.1.50', 62031))

    def test_connection_lost_unregisters(self):
        hb, sock = self._make_mock_protocol()
        proto = UsrsctpInboundProtocol(hb, sock)
        self.assertIn(('192.168.1.50', 62031), hb._sctp_transports)
        proto.connection_lost()
        self.assertNotIn(('192.168.1.50', 62031), hb._sctp_transports)

    def test_connection_lost_removes_repeater(self):
        hb, sock = self._make_mock_protocol()
        proto = UsrsctpInboundProtocol(hb, sock)

        mock_repeater = Mock()
        mock_repeater.ip = '192.168.1.50'
        mock_repeater.port = 62031
        rid = b'\x00\x04\xc4\x00'
        hb._repeaters = {rid: mock_repeater}

        proto.connection_lost()
        hb._remove_repeater.assert_called_once_with(rid, 'sctp_connection_lost')


class TestUsrsctpOutboundProtocol(unittest.TestCase):
    """Test UsrsctpOutboundProtocol interface compliance."""

    def test_write_calls_send(self):
        hb = Mock()
        hb._outbounds = {}
        sock = Mock(spec=UsrsctpSocket)
        sock.peername = ('10.0.0.1', 62031)
        sock.send = Mock()

        proto = UsrsctpOutboundProtocol(hb, 'test-link', sock)
        proto.write(b'RPTL\x00\x04\xc4\x57')
        sock.send.assert_called_once_with(b'RPTL\x00\x04\xc4\x57')

    def test_connection_lost_marks_disconnected(self):
        hb = Mock()
        outbound_state = Mock()
        outbound_state.connected = True
        hb._outbounds = {'test-link': outbound_state}

        sock = Mock(spec=UsrsctpSocket)
        sock.peername = ('10.0.0.1', 62031)
        proto = UsrsctpOutboundProtocol(hb, 'test-link', sock)
        proto.connection_lost()
        self.assertFalse(outbound_state.connected)


class TestSendCallablePattern(unittest.TestCase):
    """Verify usrsctp send callable integrates with RepeaterState."""

    def test_usrsctp_send_on_repeater_state(self):
        from hblink4.models import RepeaterState
        repeater = RepeaterState(
            repeater_id=b'\x00\x04\xc4\x00',
            ip='10.0.0.5',
            port=62031,
        )
        mock_sock = Mock(spec=UsrsctpSocket)
        mock_sock.send = Mock()

        # Assign usrsctp send callable
        repeater.send = mock_sock.send
        repeater.transport_type = 'sctp'

        repeater.send(b'MSTCL')
        mock_sock.send.assert_called_once_with(b'MSTCL')


class TestSCTPBackendFallback(unittest.TestCase):
    """Verify the kernel → usrsctp fallback chain in sctp.py."""

    def test_sctp_backend_is_valid(self):
        """SCTP_BACKEND must be 'kernel', 'usrsctp', or None."""
        self.assertIn(SCTP_BACKEND, ('kernel', 'usrsctp', None))

    def test_available_implies_backend(self):
        """If SCTP_AVAILABLE, SCTP_BACKEND must be set."""
        if SCTP_AVAILABLE:
            self.assertIsNotNone(SCTP_BACKEND)
        else:
            self.assertIsNone(SCTP_BACKEND)


class TestFindLibusrsctp(unittest.TestCase):
    """Test library discovery logic."""

    @patch('hblink4.usrsctp_transport.ctypes.util.find_library')
    @patch('os.path.exists')
    def test_falls_back_to_homebrew_path(self, mock_exists, mock_find):
        mock_find.return_value = None
        mock_exists.side_effect = lambda p: p == '/opt/homebrew/lib/libusrsctp.dylib'
        result = _find_libusrsctp()
        self.assertEqual(result, '/opt/homebrew/lib/libusrsctp.dylib')

    @patch('hblink4.usrsctp_transport.ctypes.util.find_library')
    def test_uses_find_library_first(self, mock_find):
        mock_find.return_value = '/usr/lib/libusrsctp.so'
        result = _find_libusrsctp()
        self.assertEqual(result, '/usr/lib/libusrsctp.so')


@unittest.skipUnless(USRSCTP_AVAILABLE, 'libusrsctp not installed')
class TestUsrsctpIntegration(unittest.TestCase):
    """Integration tests requiring libusrsctp. Skipped if not installed."""

    def test_library_loads(self):
        from hblink4.usrsctp_transport import _lib
        self.assertIsNotNone(_lib)

    def test_init_usrsctp(self):
        from hblink4.usrsctp_transport import _init_usrsctp, _initialized
        _init_usrsctp(udp_encap_port=9899)
        from hblink4.usrsctp_transport import _initialized as init_after
        self.assertTrue(init_after)


if __name__ == '__main__':
    unittest.main()
