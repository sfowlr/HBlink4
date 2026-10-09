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


class TestStructLayouts(unittest.TestCase):
    """ctypes layouts match usrsctp.h."""

    def test_sizes(self):
        from hblink4.usrsctp_transport import SctpRtoinfo, SctpUdpencaps, SctpPrinfo
        self.assertEqual(ctypes.sizeof(SctpRtoinfo), 16)
        # sockaddr_storage (128, 8-aligned) + uint32 + uint16, padded
        self.assertEqual(ctypes.sizeof(SctpUdpencaps), 136)
        self.assertEqual(SctpUdpencaps.sue_assoc_id.offset, 128)
        self.assertEqual(SctpUdpencaps.sue_port.offset, 132)
        self.assertEqual(ctypes.sizeof(SctpPrinfo), 8)
        self.assertEqual(SctpPrinfo.pr_value.offset, 4)


def _mock_lib():
    lib = Mock()
    lib.usrsctp_sendv.return_value = 53
    lib.usrsctp_setsockopt.return_value = 0
    lib.usrsctp_socket.return_value = 0x1234
    lib.usrsctp_connect.return_value = 0
    lib.usrsctp_get_events.return_value = 0x0002  # writable: connected
    return lib


class TestDmrdLifetime(unittest.TestCase):
    """PR-SCTP lifetime on our DMRD only; everything else reliable."""

    def _sock(self, lib, ttl):
        with patch('hblink4.usrsctp_transport._lib', lib):
            return UsrsctpSocket(0x1234, ('10.0.0.1', 62031), Mock(), Mock(), Mock(),
                                 dmrd_ttl_ms=ttl)

    def _infotype(self, call):
        return call.args[7]

    def test_dmrd_with_lifetime(self):
        from hblink4.usrsctp_transport import SCTP_SENDV_PRINFO, SCTP_PR_SCTP_TTL
        lib = _mock_lib()
        sock = self._sock(lib, 300)
        with patch('hblink4.usrsctp_transport._lib', lib):
            sock.send(b'DMRD' + b'\x00' * 49)
        call = lib.usrsctp_sendv.call_args
        self.assertEqual(self._infotype(call), SCTP_SENDV_PRINFO)
        self.assertEqual(sock._prinfo.pr_policy, SCTP_PR_SCTP_TTL)
        self.assertEqual(sock._prinfo.pr_value, 300)

    def test_other_packets_reliable(self):
        from hblink4.usrsctp_transport import SCTP_SENDV_NOINFO
        lib = _mock_lib()
        sock = self._sock(lib, 300)
        with patch('hblink4.usrsctp_transport._lib', lib):
            for pkt in (b'MSTPONG\x00\x00\x00\x01', b'RPTACK\x00\x00\x00\x01',
                        b'DMRP\x00\x00\x00\x01', b'MSTCL'):
                sock.send(pkt)
        for call in lib.usrsctp_sendv.call_args_list:
            self.assertEqual(self._infotype(call), SCTP_SENDV_NOINFO)
            self.assertIsNone(call.args[5])

    def test_no_lifetime_by_default(self):
        from hblink4.usrsctp_transport import SCTP_SENDV_NOINFO
        lib = _mock_lib()
        sock = self._sock(lib, 0)
        with patch('hblink4.usrsctp_transport._lib', lib):
            sock.send(b'DMRD' + b'\x00' * 49)
        self.assertEqual(self._infotype(lib.usrsctp_sendv.call_args), SCTP_SENDV_NOINFO)


class TestAssociationEnd(unittest.TestCase):
    """A shutdown, or an association that's gone (aborted, peer given up on),
    closes the socket and reports the loss; "nothing to read" doesn't."""

    def _drain(self, n, err):
        import errno as _errno
        lib = _mock_lib()
        lib.usrsctp_recvv.return_value = n
        loop = Mock()
        on_close = Mock()
        with patch('hblink4.usrsctp_transport._lib', lib):
            sock = UsrsctpSocket(0x1234, ('10.0.0.1', 62031), loop, Mock(), on_close)
            with patch('hblink4.usrsctp_transport.ctypes.get_errno', return_value=err):
                sock._drain_recv()
            # run what was posted to the loop
            for call in loop.call_soon_threadsafe.call_args_list:
                call.args[0](*call.args[1:])
        return lib, on_close

    def test_shutdown_closes(self):
        lib, on_close = self._drain(0, 0)
        on_close.assert_called_once()
        lib.usrsctp_close.assert_called_once_with(0x1234)

    def test_error_closes(self):
        import errno as _errno
        for err in (_errno.ECONNRESET, _errno.ENOTCONN, _errno.ETIMEDOUT):
            lib, on_close = self._drain(-1, err)
            on_close.assert_called_once()
            lib.usrsctp_close.assert_called_once_with(0x1234)

    def test_would_block_keeps_open(self):
        import errno as _errno
        lib, on_close = self._drain(-1, _errno.EAGAIN)
        on_close.assert_not_called()
        lib.usrsctp_close.assert_not_called()


class TestOutboundConnectOptions(unittest.TestCase):
    """usrsctp_connect sets RTO and the server's UDP port before connecting."""

    def _connect(self, lib, **kw):
        import asyncio
        from hblink4 import usrsctp_transport as ut
        with patch.object(ut, '_lib', lib), patch.object(ut, '_init_usrsctp'):
            return asyncio.run(ut.usrsctp_connect(Mock(), 'link', '10.0.0.1', 62031, **kw))

    def _opts(self, lib):
        """(option, struct) for each setsockopt before usrsctp_connect."""
        out = []
        for name, args, _ in lib.mock_calls:
            if name == 'usrsctp_connect':
                break
            if name == 'usrsctp_setsockopt':
                out.append((args[2], args[3]._obj))
        return out

    def test_remote_encap_port_set_before_connect(self):
        from hblink4.usrsctp_transport import SCTP_REMOTE_UDP_ENCAPS_PORT
        lib = _mock_lib()
        self._connect(lib, encap_port=9899, remote_encap_port=9900)
        encaps = [o for opt, o in self._opts(lib) if opt == SCTP_REMOTE_UDP_ENCAPS_PORT]
        self.assertEqual(len(encaps), 1)
        self.assertEqual(socket.ntohs(encaps[0].sue_port), 9900)
        self.assertEqual(bytes(encaps[0].sue_address), b'\x00' * 128)

    def test_remote_encap_port_defaults_to_ours(self):
        from hblink4.usrsctp_transport import SCTP_REMOTE_UDP_ENCAPS_PORT
        lib = _mock_lib()
        self._connect(lib, encap_port=9899)
        encaps = [o for opt, o in self._opts(lib) if opt == SCTP_REMOTE_UDP_ENCAPS_PORT]
        self.assertEqual(socket.ntohs(encaps[0].sue_port), 9899)

    def test_raw_sets_no_encap_port(self):
        from hblink4.usrsctp_transport import SCTP_REMOTE_UDP_ENCAPS_PORT
        lib = _mock_lib()
        self._connect(lib, encap_port=0)
        self.assertFalse([o for opt, o in self._opts(lib) if opt == SCTP_REMOTE_UDP_ENCAPS_PORT])

    def test_rto_set_before_connect(self):
        from hblink4.usrsctp_transport import SCTP_RTOINFO
        lib = _mock_lib()
        self._connect(lib, encap_port=9899, rto=(1000, 200, 5000))
        rto = [o for opt, o in self._opts(lib) if opt == SCTP_RTOINFO]
        self.assertEqual(len(rto), 1)
        self.assertEqual((rto[0].srto_initial, rto[0].srto_min, rto[0].srto_max), (1000, 200, 5000))

    def test_connect_error_closes_socket(self):
        lib = _mock_lib()
        lib.usrsctp_get_events.return_value = 0x0004 | 0x0002  # error (writable too)
        with self.assertRaises(OSError):
            self._connect(lib, encap_port=9899)
        lib.usrsctp_close.assert_called_once_with(0x1234)

    def test_connect_times_out(self):
        lib = _mock_lib()
        lib.usrsctp_get_events.return_value = 0
        with self.assertRaises(TimeoutError):
            self._connect(lib, encap_port=9899, timeout=0.1)
        lib.usrsctp_close.assert_called_once_with(0x1234)

    def test_lifetime_passed_to_socket(self):
        lib = _mock_lib()
        proto, send = self._connect(lib, encap_port=9899, dmrd_ttl_ms=250)
        self.assertEqual(proto.usrsctp_sock.dmrd_ttl_ms, 250)


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



@unittest.skipUnless(USRSCTP_AVAILABLE, 'libusrsctp not installed')
class TestUsrsctpLoopback(unittest.TestCase):
    """A real association in this process over UDP encapsulation: our own
    outbound connect reaching our own listener (it needs the remote UDP port
    set), the RTO both ends start with, messages both ways, and a DMRD sent
    with a lifetime."""

    def test_outbound_to_listener_over_udp(self):
        import asyncio
        from hblink4 import usrsctp_transport as ut

        if ut._initialized:
            encap = ut._encap_port
        else:
            u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            u.bind(('127.0.0.1', 0))
            encap = u.getsockname()[1]
            u.close()
            ut._init_usrsctp(encap)
        if not encap:
            self.skipTest('usrsctp initialized without UDP encapsulation')

        t = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        t.bind(('127.0.0.1', 0))
        sctp_port = t.getsockname()[1]
        t.close()
        rto = (600, 150, 3000)

        async def run():
            loop = asyncio.get_running_loop()
            got_in, got_out = asyncio.Queue(), asyncio.Queue()

            class Server:
                _sctp_transports = {}
                _repeaters = {}

                def datagram_received(self, data, addr):
                    got_in.put_nowait(data)
                    self._sctp_transports[normalize_addr(addr)].write(b'MSTPONG' + data[4:])

                def _remove_repeater(self, rid, reason):
                    pass

            client = Mock()
            client._handle_outbound_packet.side_effect = lambda n, d, a: got_out.put_nowait(d)
            server = Server()
            listener = ut.UsrsctpListener(server, '127.0.0.1', sctp_port, loop,
                                          encap_port=encap, rto=rto)
            listener.start()
            try:
                self.assertEqual(ut.get_rto(listener._listen_sock), rto)
                proto, send = await asyncio.wait_for(
                    ut.usrsctp_connect(client, 'loop', '127.0.0.1', sctp_port, encap_port=encap,
                                       remote_encap_port=encap, rto=rto, dmrd_ttl_ms=300), 10)
                self.assertEqual(ut.get_rto(proto.usrsctp_sock._sock), rto)
                send(b'RPTPING\x00\x00\x00\x01')
                self.assertEqual(await asyncio.wait_for(got_in.get(), 5), b'RPTPING\x00\x00\x00\x01')
                self.assertEqual(await asyncio.wait_for(got_out.get(), 5), b'MSTPONGING\x00\x00\x00\x01')
                # A DMRD goes with its PR-SCTP lifetime (usrsctp takes the sctp_prinfo)
                dmrd = b'DMRD' + bytes(range(49))
                send(dmrd)
                self.assertEqual(await asyncio.wait_for(got_in.get(), 5), dmrd)
                accepted = next(iter(listener._connections.values())).usrsctp_sock
                self.assertEqual(ut.get_rto(accepted._sock), rto)
                proto.usrsctp_sock.close()
            finally:
                listener._running = False
                for conn in listener._connections.values():
                    conn.usrsctp_sock.close()

        asyncio.run(run())


if __name__ == '__main__':
    unittest.main()
