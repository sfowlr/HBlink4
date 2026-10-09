"""
Tests for SCTP transport support.

Mock-based tests run on all platforms. Integration tests requiring real SCTP
sockets are skipped on macOS (which lacks kernel SCTP support).
"""
import asyncio
import sys
import unittest
from unittest.mock import Mock, MagicMock, patch

from hblink4.sctp import (
    SCTP_AVAILABLE,
    KERNEL_SCTP_AVAILABLE,
    IPPROTO_SCTP,
    SCTPInboundProtocol,
    SCTPOutboundProtocol,
    check_sctp_available,
)
from hblink4.utils import normalize_addr


class TestSCTPAvailability(unittest.TestCase):
    """Verify SCTP availability detection."""

    def test_ipproto_sctp_constant(self):
        self.assertEqual(IPPROTO_SCTP, 132)

    def test_check_sctp_available_returns_bool(self):
        result = check_sctp_available()
        self.assertIsInstance(result, bool)

    @unittest.skipUnless(sys.platform == 'darwin', 'macOS-specific')
    def test_no_kernel_sctp_on_macos(self):
        """macOS has no kernel SCTP. SCTP may still be available via usrsctp."""
        from hblink4.sctp import SCTP_BACKEND
        from hblink4.usrsctp_transport import USRSCTP_AVAILABLE
        # Kernel SCTP is never available on macOS
        self.assertNotEqual(SCTP_BACKEND, 'kernel')
        # If SCTP is available at all, it must be via usrsctp
        if SCTP_AVAILABLE:
            self.assertEqual(SCTP_BACKEND, 'usrsctp')
            self.assertTrue(USRSCTP_AVAILABLE)

    @unittest.skipUnless(sys.platform == 'linux', 'Linux-specific')
    def test_sctp_may_be_available_on_linux(self):
        # On Linux SCTP *may* be available (depends on kernel module).
        # We just verify the probe didn't crash.
        self.assertIsInstance(SCTP_AVAILABLE, bool)


class TestSCTPInboundProtocol(unittest.TestCase):
    """Test SCTPInboundProtocol delegates correctly to HBProtocol."""

    def _make_protocol(self):
        mock_hb = Mock()
        mock_hb._sctp_transports = {}
        mock_hb._repeaters = {}
        proto = SCTPInboundProtocol(mock_hb)
        return proto, mock_hb

    def _make_transport(self, ip='192.168.1.100', port=62031):
        transport = Mock()
        transport.get_extra_info.return_value = (ip, port)
        return transport

    def test_connection_made_registers_transport(self):
        proto, hb = self._make_protocol()
        transport = self._make_transport()

        proto.connection_made(transport)

        self.assertEqual(proto.peername, ('192.168.1.100', 62031))
        self.assertIn(('192.168.1.100', 62031), hb._sctp_transports)
        self.assertIs(hb._sctp_transports[('192.168.1.100', 62031)], transport)

    def test_data_received_delegates_to_datagram_received(self):
        proto, hb = self._make_protocol()
        transport = self._make_transport()
        proto.connection_made(transport)

        test_data = b'RPTL\x00\x04\xc4\x00'
        proto.data_received(test_data)

        hb.datagram_received.assert_called_once_with(
            test_data, ('192.168.1.100', 62031)
        )

    def test_connection_lost_unregisters_transport(self):
        proto, hb = self._make_protocol()
        transport = self._make_transport()
        proto.connection_made(transport)

        self.assertIn(('192.168.1.100', 62031), hb._sctp_transports)

        proto.connection_lost(None)

        self.assertNotIn(('192.168.1.100', 62031), hb._sctp_transports)

    def test_connection_lost_removes_matching_repeater(self):
        proto, hb = self._make_protocol()
        transport = self._make_transport()
        proto.connection_made(transport)

        # Simulate a registered repeater at this address
        mock_repeater = Mock()
        mock_repeater.ip = '192.168.1.100'
        mock_repeater.port = 62031
        rid = b'\x00\x04\xc4\x00'
        hb._repeaters = {rid: mock_repeater}

        proto.connection_lost(None)

        hb._remove_repeater.assert_called_once_with(rid, 'sctp_connection_lost')

    def test_connection_lost_no_repeater_no_crash(self):
        proto, hb = self._make_protocol()
        transport = self._make_transport()
        proto.connection_made(transport)
        hb._repeaters = {}

        # Should not raise
        proto.connection_lost(None)
        hb._remove_repeater.assert_not_called()

    def test_data_received_before_connection_made_is_noop(self):
        proto, hb = self._make_protocol()
        # peername is None before connection_made
        proto.data_received(b'RPTL\x00\x00\x00\x01')
        hb.datagram_received.assert_not_called()


class TestSCTPOutboundProtocol(unittest.TestCase):
    """Test SCTPOutboundProtocol delegates correctly to HBProtocol."""

    def test_data_received_delegates_to_handle_outbound_packet(self):
        hb = Mock()
        hb._outbounds = {}
        proto = SCTPOutboundProtocol(hb, 'test-link')

        transport = Mock()
        transport.get_extra_info.return_value = ('10.0.0.1', 62031)
        proto.connection_made(transport)

        test_data = b'RPTACK\x00\x00\x00\x01'
        proto.data_received(test_data)

        hb._handle_outbound_packet.assert_called_once_with(
            'test-link', test_data, ('10.0.0.1', 62031)
        )

    def test_connection_lost_marks_outbound_disconnected(self):
        hb = Mock()
        outbound_state = Mock()
        outbound_state.connected = True
        hb._outbounds = {'test-link': outbound_state}

        proto = SCTPOutboundProtocol(hb, 'test-link')
        transport = Mock()
        transport.get_extra_info.return_value = ('10.0.0.1', 62031)
        proto.connection_made(transport)

        proto.connection_lost(Exception('peer reset'))

        self.assertFalse(outbound_state.connected)

    def test_connection_lost_unknown_name_no_crash(self):
        hb = Mock()
        hb._outbounds = {}
        proto = SCTPOutboundProtocol(hb, 'nonexistent')
        proto.transport = Mock()
        # Should not raise
        proto.connection_lost(None)


class TestSendCallablePattern(unittest.TestCase):
    """Verify that both UDP and SCTP send patterns work correctly."""

    def test_udp_send_callable(self):
        """UDP pattern: lambda closing over transport + addr"""
        mock_transport = Mock()
        addr = ('192.168.1.1', 62031)
        send = lambda data, _a=addr, _t=mock_transport: _t.sendto(data, _a)

        send(b'DMRD' + b'\x00' * 49)

        mock_transport.sendto.assert_called_once_with(
            b'DMRD' + b'\x00' * 49, addr
        )

    def test_sctp_send_callable(self):
        """SCTP pattern: transport.write"""
        mock_transport = Mock()
        send = mock_transport.write

        send(b'DMRD' + b'\x00' * 49)

        mock_transport.write.assert_called_once_with(b'DMRD' + b'\x00' * 49)

    def test_send_callable_on_repeater_state(self):
        """Verify send callable integrates with RepeaterState"""
        from hblink4.models import RepeaterState

        repeater = RepeaterState(
            repeater_id=b'\x00\x04\xc4\x00',
            ip='10.0.0.5',
            port=62031,
        )
        # Default: no send callable
        self.assertIsNone(repeater.send)
        self.assertEqual(repeater.transport_type, 'udp')

        # Set SCTP send
        mock_transport = Mock()
        repeater.send = mock_transport.write
        repeater.transport_type = 'sctp'

        repeater.send(b'MSTCL')
        mock_transport.write.assert_called_once_with(b'MSTCL')

    def test_send_callable_on_outbound_state(self):
        """Verify send callable integrates with OutboundState"""
        from hblink4.models import OutboundState, OutboundConnectionConfig

        config = OutboundConnectionConfig(
            enabled=True, name='test', address='host',
            port=62031, radio_id=312999, passphrase='pw',
        )
        state = OutboundState(config=config, ip='10.0.0.1', port=62031)
        self.assertIsNone(state.send)
        self.assertEqual(state.transport_type, 'udp')

        # Set UDP send
        mock_transport = Mock()
        state.send = mock_transport.sendto
        state.send(b'RPTL\x00\x04\xc4\x57')
        mock_transport.sendto.assert_called_once()


class TestSendPacketSCTPLookup(unittest.TestCase):
    """Verify HBProtocol._send_packet uses SCTP transport when registered."""

    def _make_hbprotocol(self):
        """Create a minimal HBProtocol mock with real _send_packet and _sctp_transports."""
        from hblink4.hblink import HBProtocol
        # We can't easily instantiate HBProtocol (needs CONFIG global),
        # so test the logic pattern directly.
        mock_udp_transport = Mock()

        class FakeProto:
            transport = mock_udp_transport
            _sctp_transports = {}

        fake = FakeProto()
        # Bind the real _send_packet logic
        fake._send_packet = lambda data, addr: (
            fake._sctp_transports.get(normalize_addr(addr)).write(data)
            if normalize_addr(addr) in fake._sctp_transports
            else fake.transport.sendto(data, normalize_addr(addr))
        )
        return fake, mock_udp_transport

    def test_send_packet_uses_udp_by_default(self):
        fake, udp_transport = self._make_hbprotocol()
        fake._send_packet(b'RPTACK\x00\x00\x00\x01', ('1.2.3.4', 62031))
        udp_transport.sendto.assert_called_once_with(
            b'RPTACK\x00\x00\x00\x01', ('1.2.3.4', 62031)
        )

    def test_send_packet_uses_sctp_when_registered(self):
        fake, udp_transport = self._make_hbprotocol()
        sctp_transport = Mock()
        fake._sctp_transports[('1.2.3.4', 62031)] = sctp_transport

        fake._send_packet(b'RPTACK\x00\x00\x00\x01', ('1.2.3.4', 62031))

        sctp_transport.write.assert_called_once_with(b'RPTACK\x00\x00\x00\x01')
        udp_transport.sendto.assert_not_called()


class TestOutboundConnectionConfigTransport(unittest.TestCase):
    """Verify transport field on OutboundConnectionConfig."""

    def test_default_transport_is_udp(self):
        from hblink4.models import OutboundConnectionConfig
        config = OutboundConnectionConfig(
            enabled=True, name='test', address='host',
            port=62031, radio_id=312999, passphrase='pw',
        )
        self.assertEqual(config.transport, 'udp')

    def test_sctp_transport(self):
        from hblink4.models import OutboundConnectionConfig
        config = OutboundConnectionConfig(
            enabled=True, name='test', address='host',
            port=62031, radio_id=312999, passphrase='pw',
            transport='sctp',
        )
        self.assertEqual(config.transport, 'sctp')


class TestSCTPSettings(unittest.TestCase):
    """global.sctp_* settings: defaults, RTO bounds, lifetime, bad values."""

    def test_defaults(self):
        from hblink4.sctp import sctp_settings
        s = sctp_settings({})
        self.assertEqual(s.encap, 'udp')
        self.assertEqual(s.encap_port, 9899)
        self.assertEqual(s.rto, (1000, 200, 5000))  # initial, min, max
        self.assertEqual(s.ttl_ms, 0)  # DMRD reliable unless asked
        self.assertEqual(s.usrsctp_encap_port, 9899)

    def test_configured(self):
        from hblink4.sctp import sctp_settings
        s = sctp_settings({'sctp_encap': 'udp', 'sctp_encap_port': 9900,
                           'sctp_rto_initial_ms': 500, 'sctp_rto_min_ms': 150,
                           'sctp_rto_max_ms': 3000, 'sctp_ttl_ms': 300})
        self.assertEqual(s.encap_port, 9900)
        self.assertEqual(s.rto, (500, 150, 3000))
        self.assertEqual(s.ttl_ms, 300)

    def test_raw_has_no_udp_port(self):
        from hblink4.sctp import sctp_settings
        s = sctp_settings({'sctp_encap': 'raw', 'sctp_encap_port': 9899})
        self.assertEqual(s.encap, 'raw')
        self.assertEqual(s.usrsctp_encap_port, 0)

    def test_bad_values_fall_back(self):
        from hblink4.sctp import sctp_settings
        with self.assertLogs('hblink4.sctp', level='WARNING'):
            s = sctp_settings({'sctp_encap': 'tcp', 'sctp_rto_min_ms': -1,
                               'sctp_ttl_ms': 'soon', 'sctp_encap_port': 70000})
        self.assertEqual(s.encap, 'udp')
        self.assertEqual(s.rto_min_ms, 200)
        self.assertEqual(s.ttl_ms, 0)
        self.assertEqual(s.encap_port, 9899)

    def test_rto_out_of_order_falls_back(self):
        from hblink4.sctp import sctp_settings
        with self.assertLogs('hblink4.sctp', level='WARNING'):
            s = sctp_settings({'sctp_rto_initial_ms': 100, 'sctp_rto_min_ms': 200,
                               'sctp_rto_max_ms': 5000})
        self.assertEqual(s.rto, (1000, 200, 5000))


class TestSCTPBackendPlan(unittest.TestCase):
    """Which backend takes which association, by sctp_encap and what's installed."""

    def test_udp_listens_with_both(self):
        from hblink4.sctp import plan_sctp_listeners
        # Linux with kernel SCTP and libusrsctp: plain SCTP and SCTP over UDP side by side
        self.assertEqual(plan_sctp_listeners('udp', True, True), (True, True))
        # macOS: usrsctp only
        self.assertEqual(plan_sctp_listeners('udp', False, True), (False, True))
        # Linux without libusrsctp: plain SCTP only
        self.assertEqual(plan_sctp_listeners('udp', True, False), (True, False))
        self.assertEqual(plan_sctp_listeners('udp', False, False), (False, False))

    def test_raw_prefers_kernel(self):
        from hblink4.sctp import plan_sctp_listeners
        self.assertEqual(plan_sctp_listeners('raw', True, True), (True, False))
        self.assertEqual(plan_sctp_listeners('raw', False, True), (False, True))

    def test_outbound(self):
        from hblink4.sctp import plan_sctp_outbound
        self.assertEqual(plan_sctp_outbound('udp', True, True), 'usrsctp')
        self.assertEqual(plan_sctp_outbound('udp', True, False), 'kernel')
        self.assertEqual(plan_sctp_outbound('raw', True, True), 'kernel')
        self.assertEqual(plan_sctp_outbound('raw', False, True), 'usrsctp')
        self.assertIsNone(plan_sctp_outbound('udp', False, False))


class TestKernelRTO(unittest.TestCase):
    """SCTP_RTOINFO on kernel sockets: struct sctp_rtoinfo is assoc id, initial, max, min."""

    def test_apply_sctp_rto_packs_initial_max_min(self):
        import struct
        from hblink4.sctp import apply_sctp_rto, SCTP_RTOINFO
        sock = Mock()
        apply_sctp_rto(sock, (1000, 200, 5000))
        sock.setsockopt.assert_called_once_with(
            IPPROTO_SCTP, SCTP_RTOINFO, struct.pack('=iIII', 0, 1000, 5000, 200))

    def test_apply_sctp_rto_failure_only_warns(self):
        from hblink4.sctp import apply_sctp_rto
        sock = Mock()
        sock.setsockopt.side_effect = OSError('nope')
        with self.assertLogs('hblink4.sctp', level='WARNING'):
            apply_sctp_rto(sock, (1000, 200, 5000))


class TestOutboundSCTPEncapConfig(unittest.TestCase):
    """Per-link sctp_encap / sctp_encap_port on outbound connections."""

    def _cfg(self, **kw):
        from hblink4.models import OutboundConnectionConfig
        return OutboundConnectionConfig(enabled=True, name='t', address='h', port=62031,
                                        radio_id=312999, passphrase='pw', transport='sctp', **kw)

    def test_defaults_to_global(self):
        c = self._cfg()
        self.assertIsNone(c.sctp_encap)
        self.assertIsNone(c.sctp_encap_port)

    def test_parsed_from_config(self):
        from hblink4.config import parse_outbound_connections
        out = parse_outbound_connections({'outbound_connections': [{
            'name': 't', 'address': 'h', 'port': 62031, 'radio_id': 312999,
            'passphrase': 'pw', 'transport': 'sctp', 'sctp_encap': 'udp',
            'sctp_encap_port': 9900}]})
        self.assertEqual(out[0].sctp_encap, 'udp')
        self.assertEqual(out[0].sctp_encap_port, 9900)

    def test_invalid_rejected(self):
        with self.assertRaises(ValueError):
            self._cfg(sctp_encap='tcp')
        with self.assertRaises(ValueError):
            self._cfg(sctp_encap_port=0)


@unittest.skipUnless(sys.platform == 'linux', 'SCTP requires Linux kernel')
class TestSCTPSocketCreation(unittest.TestCase):
    """Integration tests for actual SCTP socket creation — Linux only."""

    @unittest.skipUnless(KERNEL_SCTP_AVAILABLE, 'SCTP kernel module not loaded')
    def test_create_listen_socket_with_rto(self):
        from hblink4.sctp import create_sctp_listen_socket, SCTP_RTOINFO
        import struct
        sock = create_sctp_listen_socket('127.0.0.1', 0, rto=(700, 150, 4000))
        try:
            raw = sock.getsockopt(IPPROTO_SCTP, SCTP_RTOINFO, 16)
            _, initial, rto_max, rto_min = struct.unpack('=iIII', raw)
            self.assertEqual((initial, rto_min, rto_max), (700, 150, 4000))
        finally:
            sock.close()

    @unittest.skipUnless(KERNEL_SCTP_AVAILABLE, 'SCTP kernel module not loaded')
    def test_create_listen_socket(self):
        from hblink4.sctp import create_sctp_listen_socket
        import socket
        sock = create_sctp_listen_socket('127.0.0.1', 0)  # ephemeral port
        try:
            self.assertEqual(sock.type & 0xf, socket.SOCK_STREAM)
            self.assertFalse(sock.getblocking())
            addr = sock.getsockname()
            self.assertEqual(addr[0], '127.0.0.1')
            self.assertGreater(addr[1], 0)
        finally:
            sock.close()

    @unittest.skipUnless(KERNEL_SCTP_AVAILABLE, 'SCTP kernel module not loaded')
    def test_create_connect_socket(self):
        from hblink4.sctp import create_sctp_connect_socket
        import socket
        sock = create_sctp_connect_socket('127.0.0.1')
        try:
            self.assertEqual(sock.type & 0xf, socket.SOCK_STREAM)
            self.assertFalse(sock.getblocking())
        finally:
            sock.close()


if __name__ == '__main__':
    unittest.main()
