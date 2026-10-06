"""
Unit (private) call routing: only the target radio's channel transmits.

- a radio heard on a TX-capable peer → that peer, on the radio's last slot
- a radio heard on a receive-only peer (SDR) → the TX-capable peer on the
  same frequency / color code, on the radio's last slot
- unknown radio or no matching TX peer → nothing is sent (no flood), unless
  global.unit_call_flood
- static subscribers (e.g. a console peer's radio id) always route to it
- external last-heard reports (MQTT) feed the cache, newest wins
"""
import json
import os
import sys
from time import time
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hblink4 import hblink
from hblink4.hblink import HBProtocol, RepeaterState
from hblink4.external_last_heard import ExternalLastHeard
from hblink4.user_cache import UserCache
from hblink4.utils import parse_freq_hz

CONSOLE_PEER, MODEM1, MODEM2, SDR1 = 3129001, 311001, 311002, 3128001
CONSOLE_RADIO, RADIO = 3129000, 3100123


def rid(n):
    return n.to_bytes(4, 'big')


def sid(n):
    return n.to_bytes(3, 'big')


def peer(hb, n, freq=None, cc=1, tx=True, unit=True):
    r = RepeaterState(repeater_id=rid(n), ip='127.0.0.1', port=50000 + n % 1000)
    r.connection_state = 'connected'
    r.unit_calls_enabled = unit
    r.tx_capable = tx
    if freq:
        r.rx_freq = str(freq).encode()
        r.tx_freq = str(freq + 5_000_000).encode()   # repeater split
    r.colorcode = str(cc).encode()
    r.sent = []
    r.send = r.sent.append
    hb._repeaters[r.repeater_id] = r
    return r


def make_hb():
    hb = HBProtocol()
    hb._events.emitted = []
    hb._events.emit = lambda kind, data: hb._events.emitted.append((kind, data))
    peer(hb, CONSOLE_PEER)
    peer(hb, MODEM1, freq=461_687_500)
    peer(hb, MODEM2, freq=462_100_000)
    peer(hb, SDR1, freq=461_687_500, tx=False, unit=False)
    return hb


def heard(hb, radio, repeater, slot):
    hb._user_cache.update(radio_id=radio, repeater_id=repeater, callsign='', slot=slot,
                          talkgroup=9, **hb._peer_channel(repeater))


def route(hb, src_peer=CONSOLE_PEER, slot=1, dst=RADIO, src=CONSOLE_RADIO, stream=b'\x01\x02\x03\x04'):
    return hb._calculate_unit_call_targets(rid(src_peer), slot, sid(src), sid(dst), stream)


def test_heard_on_tx_peer_routes_there_on_its_slot():
    hb = make_hb()
    heard(hb, RADIO, MODEM1, 2)
    assert route(hb, slot=1) == ({rid(MODEM1)}, False, {rid(MODEM1): 2})
    assert route(hb, slot=2) == ({rid(MODEM1)}, False, {})


def test_heard_on_sdr_routes_to_modem_on_same_frequency():
    hb = make_hb()
    heard(hb, RADIO, SDR1, 1)
    assert route(hb) == ({rid(MODEM1)}, False, {})


def test_frequency_match_also_accepts_peer_tx_frequency_and_checks_color_code():
    hb = make_hb()
    hb._user_cache.update(radio_id=RADIO, repeater_id=0, callsign='', slot=1, talkgroup=0,
                          freq=462_100_000 + 5_000_000, colorcode=1, source='voice-ext')
    assert route(hb)[0] == {rid(MODEM2)}
    hb._user_cache.update(radio_id=RADIO, repeater_id=0, callsign='', slot=1, talkgroup=0,
                          freq=462_100_000, colorcode=7, source='voice-ext')
    assert route(hb) == (set(), False, {})


def test_unknown_radio_or_no_channel_match_sends_nothing():
    hb = make_hb()
    assert route(hb) == (set(), False, {})
    hb._user_cache.update(radio_id=RADIO, repeater_id=0, callsign='', slot=1, talkgroup=0,
                          freq=450_000_000, source='voice-ext')
    assert route(hb) == (set(), False, {})


def test_flood_is_opt_in_and_skips_receive_only_peers():
    hb = make_hb()
    with patch.dict(hblink.CONFIG, {'global': {'unit_call_flood': True}}):
        targets, broadcast, slots = route(hb)
    assert broadcast and targets == {rid(MODEM1), rid(MODEM2)}


def test_busy_target_slot_is_not_used():
    hb = make_hb()
    heard(hb, RADIO, MODEM1, 2)
    hb._is_slot_busy = lambda repeater_id, slot, *a, **k: (repeater_id, slot) == (rid(MODEM1), 2)
    assert route(hb) == (set(), False, {})


def test_static_subscriber_routes_to_its_peer_on_caller_slot():
    hb = make_hb()
    hb._user_cache.pin(CONSOLE_RADIO, CONSOLE_PEER)
    heard(hb, CONSOLE_RADIO, MODEM2, 1)       # pins win over activity
    targets = hb._calculate_unit_call_targets(rid(MODEM1), 2, sid(RADIO), sid(CONSOLE_RADIO), b'\x05\x06\x07\x08')
    assert targets == ({rid(CONSOLE_PEER)}, False, {})


def test_forwarded_packets_carry_the_target_slot():
    hb = make_hb()
    heard(hb, RADIO, MODEM1, 2)
    stream_id = b'\xaa\xbb\xcc\xdd'
    console = hb._repeaters[rid(CONSOLE_PEER)]
    assert hb._handle_unit_stream_start(console, sid(CONSOLE_RADIO), sid(RADIO), 1, stream_id)
    pkt = bytearray(55)
    pkt[0:4] = b'DMRD'
    pkt[5:8] = sid(CONSOLE_RADIO)
    pkt[8:11] = sid(RADIO)
    pkt[11:15] = rid(CONSOLE_PEER)
    pkt[15] = 0x40 | 0x10        # TS1, private, voice sync frame
    pkt[16:20] = stream_id
    hb._forward_stream(bytes(pkt), rid(CONSOLE_PEER), 1, sid(CONSOLE_RADIO), sid(RADIO), stream_id)
    modem = hb._repeaters[rid(MODEM1)]
    assert len(modem.sent) == 1 and modem.sent[0][15] & 0x80     # sent on TS2
    assert not hb._repeaters[rid(MODEM2)].sent and not hb._repeaters[rid(SDR1)].sent
    assert modem.get_slot_stream(2) is not None and modem.get_slot_stream(1) is None


def test_unroutable_call_emits_event():
    hb = make_hb()
    console = hb._repeaters[rid(CONSOLE_PEER)]
    assert hb._handle_unit_stream_start(console, sid(CONSOLE_RADIO), sid(RADIO), 1, b'\x01\x01\x01\x01')
    assert ('unit_call_unroutable', {'repeater_id': CONSOLE_PEER, 'slot': 1, 'src_id': CONSOLE_RADIO,
                                     'dst_id': RADIO}) in hb._events.emitted


# ── external last-heard (MQTT reports) ────────────────────────────────────────

def report(**r):
    return json.dumps(r).encode()


def test_external_reports_feed_the_cache_and_newest_wins():
    cache = UserCache(timeout_seconds=10 ** 9)
    feed = ExternalLastHeard(cache, 'hblink4/last_heard')
    feed.handle('hblink4/last_heard', report(radio_id=RADIO, freq='462.10000', colorcode=2, slot=2,
                                             at='2026-09-27T13:05:00Z', source='ars'))
    e = cache.lookup(RADIO)
    assert (e.freq, e.colorcode, e.slot, e.source, e.repeater_id) == (462_100_000, 2, 2, 'ars-ext', 0)

    # Heard here more recently than the next report: the older report is ignored.
    cache.update(radio_id=RADIO, repeater_id=MODEM1, callsign='', slot=1, talkgroup=9, freq=461_687_500)
    feed.handle('hblink4/last_heard/x', report(radio_id=RADIO, freq=462100000, slot=2, at=1790000000))
    assert cache.lookup(RADIO).repeater_id == MODEM1


def test_external_reports_in_a_list_and_bad_ones_skipped():
    cache = UserCache(timeout_seconds=10 ** 9)
    feed = ExternalLastHeard(cache)
    feed.handle('hblink4/last_heard', json.dumps([
        {'radio_id': 1, 'freq': 461.6875}, {'radio_id': 'x', 'freq': 1}, {'radio_id': 2},
        {'radio_id': 3, 'freq': 461687500, 'slot': 7}]).encode())
    feed.handle('hblink4/last_heard', b'not json')
    assert cache.lookup(1).freq == 461_687_500 and cache.lookup(1).slot == 0
    assert cache.lookup(2) is None and cache.lookup(3).slot == 0


def test_externally_reported_radio_routes_by_frequency():
    hb = make_hb()
    ExternalLastHeard(hb._user_cache).handle('hblink4/last_heard', report(radio_id=RADIO, freq=461687500, slot=2))
    assert route(hb) == ({rid(MODEM1)}, False, {rid(MODEM1): 2})


def test_parse_freq_hz():
    assert parse_freq_hz(b'461687500') == 461_687_500
    assert parse_freq_hz('461.68750') == 461_687_500
    assert parse_freq_hz(b'') is None and parse_freq_hz('abc') is None


def test_dmrc_peer_restarting_on_a_new_port_is_followed():
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
    hb = make_hb()
    out = []
    hb.transport = type('T', (), {'sendto': lambda self, data, addr: out.append((data[:4], addr))})()
    from hblink4.access_control import RepeaterMatcher
    hb._matcher = RepeaterMatcher({'repeater_configurations': {'patterns': [
        {'name': 'p', 'description': '', 'match': {'ids': [7777]},
         'config': {'passphrase': 'x', 'default_unit_calls': True}}]}})
    body = ('%-8.8s%09u%09u%02u%02u%c%-40.40s%-40.40s' % ('TEST', 0, 0, 0, 1, '3', 'v', 's')).encode()
    hb._handle_dmrc(rid(7777), b'DMRC' + rid(7777) + body, ('10.0.0.5', 40000))
    r = hb._repeaters[rid(7777)]
    assert r.connection_state == 'connected'
    hb._handle_dmrc(rid(7777), b'DMRC' + rid(7777) + body, ('10.0.0.5', 40001))
    assert r.sockaddr == ('10.0.0.5', 40001)
    out.clear()
    r.send(b'DMRDxxxx')
    assert out == [(b'DMRD', ('10.0.0.5', 40001))]
