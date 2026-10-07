"""
Roaming transceivers: a simplex radio HBlink4 retunes per unit call (DMRT), for
radios on channels no fixed peer transmits on.

- used only when no fixed TX peer is on the radio's channel (busy or not)
- only for channels in global.roaming_channels, and only when the channel is quiet
- always TS2; a roamer already on the channel first, then by priority, equal ones taking turns
- one hearing a call: never used, used last, or used as needed (roaming_interrupt_rx)
- DMRK on air → its channel is recorded; refused or unanswered → the next roamer, the
  call replayed; channel busy (or no roamer left) → the call is dropped, reported
- never a group-call target
"""
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hblink4 import hblink
from hblink4.hblink import HBProtocol
from hblink4.models import StreamState

from test_unit_call_routing import CONSOLE_PEER, CONSOLE_RADIO, MODEM1, RADIO, SDR1, make_hb, peer, rid, route, sid

ROAM1, ROAM2 = 3127001, 3127002
SIMPLEX = 462_562_500            # no fixed transmitter here
CHANNELS = {'global': {'roaming_channels': [
    {'freq': '462.5625', 'cc': 1, 'power': 100},
    {'freq': 461.6875, 'cc': 1},               # MODEM1's channel: a fixed peer covers it
]}}


def roamer(hb, n, freq=None, cc=1):
    r = peer(hb, n, cc=cc)
    r.roaming = True
    if freq:
        r.rx_freq = r.tx_freq = str(freq).encode()
    return r


def heard_at(hb, freq, cc=1, slot=1):
    hb._user_cache.update(radio_id=RADIO, repeater_id=0, callsign='', slot=slot, talkgroup=0,
                          freq=freq, colorcode=cc, source='voice-ext')


def dmrt(r):
    return [p for p in r.sent if p[:4] == b'DMRT']


def test_a_radio_on_an_uncovered_channel_goes_out_the_roamer_on_ts2():
    hb = make_hb()
    r = roamer(hb, ROAM1)
    heard_at(hb, SIMPLEX)
    with patch.dict(hblink.CONFIG, CHANNELS):
        assert route(hb, slot=1) == ({rid(ROAM1)}, False, {rid(ROAM1): 2})
    [pkt] = dmrt(r)
    assert pkt == (b'DMRT' + rid(ROAM1) + b'\x01\x02\x03\x04' + SIMPLEX.to_bytes(4, 'big') + bytes([1, 100, 0]))


def test_never_roams_onto_a_channel_a_fixed_peer_covers_even_when_its_busy():
    hb = make_hb()
    r = roamer(hb, ROAM1)
    heard_at(hb, 461_687_500)
    hb._is_slot_busy = lambda repeater_id, slot, *a, **k: repeater_id == rid(MODEM1)
    with patch.dict(hblink.CONFIG, CHANNELS):
        assert route(hb) == (set(), False, {})
    assert not dmrt(r)


def test_only_allowed_channels_and_matching_color_codes():
    hb = make_hb()
    r = roamer(hb, ROAM1)
    heard_at(hb, 451_000_000)
    with patch.dict(hblink.CONFIG, CHANNELS):
        assert route(hb) == (set(), False, {})
        heard_at(hb, SIMPLEX, cc=5)
        assert route(hb) == (set(), False, {})
    heard_at(hb, SIMPLEX)
    assert route(hb) == (set(), False, {})              # no roaming_channels configured
    assert not dmrt(r)


def test_a_channel_with_traffic_on_it_is_left_alone():
    hb = make_hb()
    roamer(hb, ROAM1)
    sdr = hb._repeaters[rid(SDR1)]
    sdr.rx_freq = str(SIMPLEX).encode()
    sdr.set_slot_stream(1, StreamState(repeater_id=rid(SDR1), rf_src=sid(1), dst_id=sid(2), slot=1,
                                       start_time=0, last_seen=0, stream_id=b'\x09' * 4))
    heard_at(hb, SIMPLEX)
    with patch.dict(hblink.CONFIG, CHANNELS):
        assert route(hb) == (set(), False, {})
        sdr.get_slot_stream(1).ended = True
        assert route(hb)[0] == {rid(ROAM1)}


def test_prefers_a_roamer_already_on_the_channel_and_skips_a_busy_one():
    hb = make_hb()
    roamer(hb, ROAM1)
    on_channel = roamer(hb, ROAM2, freq=SIMPLEX)
    heard_at(hb, SIMPLEX)
    with patch.dict(hblink.CONFIG, CHANNELS):
        assert route(hb)[0] == {rid(ROAM2)}
        hb._is_slot_busy = lambda repeater_id, slot, *a, **k: repeater_id == rid(ROAM2) and slot == 1
        assert route(hb)[0] == {rid(ROAM1)}
        hb._is_slot_busy = lambda repeater_id, *a, **k: repeater_id in (rid(ROAM1), rid(ROAM2))
        assert route(hb) == (set(), False, {})
    assert on_channel.roaming


def test_roamers_never_get_group_calls_or_floods():
    hb = make_hb()
    roamer(hb, ROAM1)
    assert rid(ROAM1) not in hb._calculate_stream_targets(rid(MODEM1), 1, sid(9), b'\x00\x00\x01', sid(1))
    with patch.dict(hblink.CONFIG, {'global': {'unit_call_flood': True}}):
        targets, broadcast, _ = route(hb)
    assert broadcast and rid(ROAM1) not in targets


def start_call(hb, stream_id=b'\xaa\xbb\xcc\xdd'):
    console = hb._repeaters[rid(CONSOLE_PEER)]
    assert hb._handle_unit_stream_start(console, sid(CONSOLE_RADIO), sid(RADIO), 1, stream_id)
    pkt = bytearray(55)
    pkt[0:4] = b'DMRD'
    pkt[5:8], pkt[8:11], pkt[11:15] = sid(CONSOLE_RADIO), sid(RADIO), rid(CONSOLE_PEER)
    pkt[15] = 0x40 | 0x10
    pkt[16:20] = stream_id
    hb._forward_stream(bytes(pkt), rid(CONSOLE_PEER), 1, sid(CONSOLE_RADIO), sid(RADIO), stream_id)
    return console.get_slot_stream(1)


def dmrk(stream_id, status, freq=SIMPLEX, cc=1, peer_id=ROAM1):
    return b'DMRK' + rid(peer_id) + stream_id + bytes([status]) + freq.to_bytes(4, 'big') + bytes([cc])


def test_on_air_ack_records_the_channel():
    hb = make_hb()
    r = roamer(hb, ROAM1)
    heard_at(hb, SIMPLEX)
    with patch.dict(hblink.CONFIG, CHANNELS):
        start_call(hb)
    dmrd = [p for p in r.sent if p[:4] == b'DMRD']
    assert len(dmrd) == 1 and dmrd[0][15] & 0x80                   # forwarded, on TS2
    hb._handle_roaming_ack(rid(ROAM1), dmrk(b'\xaa\xbb\xcc\xdd', 0))
    assert (r.tx_freq, r.colorcode) == (str(SIMPLEX).encode(), b'01')          # as DMRC writes them
    assert ('roaming_on_air', {'repeater_id': ROAM1, 'stream_id': 'aabbccdd', 'freq': SIMPLEX,
                               'colorcode': 1}) in hb._events.emitted
    assert ('repeater_channel', {'repeater_id': ROAM1, 'rx_freq': str(SIMPLEX), 'tx_freq': str(SIMPLEX),
                                 'colorcode': '01', 'latitude': None, 'longitude': None,
                                 'height': None}) in hb._events.emitted


def test_refused_ack_drops_the_call_frees_the_roamer_and_reports_it():
    hb = make_hb()
    r = roamer(hb, ROAM1)
    heard_at(hb, SIMPLEX)
    with patch.dict(hblink.CONFIG, CHANNELS):
        stream = start_call(hb)
    assert r.get_slot_stream(2) is not None
    hb._handle_roaming_ack(rid(ROAM1), dmrk(b'\xaa\xbb\xcc\xdd', 1))
    assert stream.target_repeaters == set() and r.get_slot_stream(2) is None
    assert ('unit_call_unroutable', {'repeater_id': CONSOLE_PEER, 'slot': 1, 'src_id': CONSOLE_RADIO,
                                     'dst_id': RADIO, 'reason': 'channel busy'}) in hb._events.emitted


def test_a_roamers_keepalive_carries_where_its_tuned():
    hb = make_hb()
    out = []
    hb.transport = type('T', (), {'sendto': lambda self, data, addr: out.append(data)})()
    from hblink4.access_control import RepeaterMatcher
    hb._matcher = RepeaterMatcher({'repeater_configurations': {'patterns': [
        {'name': 'roamers', 'description': '', 'match': {'ids': [ROAM1]},
         'config': {'passphrase': 'x', 'default_unit_calls': True, 'roaming': True}}]}})

    def dmrc(freq):
        body = ('%-8.8s%09u%09u%02u%02u%c%-40.40s%-40.40s' % ('ROAM', freq, freq, 0, 1, '2', 'v', 's')).encode()
        return b'DMRC' + rid(ROAM1) + body
    hb._handle_dmrc(rid(ROAM1), dmrc(0), ('10.0.0.9', 40000))
    r = hb._repeaters[rid(ROAM1)]
    assert r.roaming and r.unit_calls_enabled
    hb._handle_dmrc(rid(ROAM1), dmrc(SIMPLEX), ('10.0.0.9', 40000))
    assert r.tx_freq == str(SIMPLEX).encode()


def test_a_radio_heard_by_the_roamer_on_its_idle_channel_is_answered_through_it():
    hb = make_hb()
    r = roamer(hb, ROAM1, freq=SIMPLEX)                  # sitting on its idle channel, receiving
    hb._user_cache.update(radio_id=RADIO, repeater_id=ROAM1, callsign='', slot=2, talkgroup=9990199,
                          **hb._peer_channel(ROAM1))
    with patch.dict(hblink.CONFIG, CHANNELS):
        assert route(hb) == ({rid(ROAM1)}, False, {rid(ROAM1): 2})
        # …but not while it's still hearing that radio's call.
        r.set_slot_stream(2, StreamState(repeater_id=rid(ROAM1), rf_src=sid(RADIO), dst_id=sid(9990199),
                                         slot=2, start_time=0, last_seen=0, stream_id=b'\x07' * 4))
        assert route(hb, stream=b'\x08' * 4) == (set(), False, {})


OTHER = 467_375_000             # a roamer's idle channel, away from SIMPLEX


def hearing(r, stream_id=b'\x07' * 4):
    """`r` is receiving a call on its own channel."""
    r.set_slot_stream(2, StreamState(repeater_id=r.repeater_id, rf_src=sid(1234), dst_id=sid(9990199), slot=2,
                                     start_time=0, last_seen=0, stream_id=stream_id))


def picked(hb, stream):
    targets = route(hb, stream=stream)[0]
    return rid_to_int(next(iter(targets))) if targets else None


def rid_to_int(b):
    return int.from_bytes(b, 'big')


def test_priority_then_turns_and_one_on_the_channel_always_wins():
    hb = make_hb()
    r1, r2, r3 = roamer(hb, ROAM1, freq=OTHER), roamer(hb, ROAM2, freq=OTHER), roamer(hb, 3127003, freq=OTHER)
    r1.roaming_priority = 200                    # a backup
    heard_at(hb, SIMPLEX)
    with patch.dict(hblink.CONFIG, CHANNELS):
        assert [picked(hb, bytes([i]) * 4) for i in range(1, 5)] == [ROAM2, 3127003, ROAM2, 3127003]
        r1.rx_freq = r1.tx_freq = str(SIMPLEX).encode()   # the backup is sitting on the channel
        assert picked(hb, b'\x05' * 4) == ROAM1


def test_a_roamer_hearing_a_call_never_last_resort_or_as_needed():
    hb = make_hb()
    r1, r2 = roamer(hb, ROAM1, freq=OTHER), roamer(hb, ROAM2, freq=OTHER)
    r1.roaming_priority, r2.roaming_priority = 10, 100
    hearing(r1)
    heard_at(hb, SIMPLEX)
    with patch.dict(hblink.CONFIG, CHANNELS):
        assert picked(hb, b'\x01' * 4) == ROAM2                      # default: never
        r1.roaming_interrupt_rx = 'last_resort'
        assert picked(hb, b'\x02' * 4) == ROAM2
        r2.roaming_cooldown_until = 1e12                             # ROAM2 unavailable
        assert picked(hb, b'\x03' * 4) == ROAM1
        assert dmrt(r1)[-1][18] == hblink.ROAMING_PREEMPT              # told to drop what it hears
        r2.roaming_cooldown_until = 0
        r1.roaming_interrupt_rx = 'as_needed'
        assert picked(hb, b'\x04' * 4) == ROAM1                      # its priority wins
        r1.roaming_interrupt_rx = None
    with patch.dict(hblink.CONFIG, {'global': {**CHANNELS['global'], 'roaming_interrupt_rx': 'as_needed'}}):
        assert picked(hb, b'\x05' * 4) == ROAM1                      # the global default
    assert dmrt(r2)[0][18] == 0


def test_a_roamer_sending_a_call_is_never_interrupted():
    hb = make_hb()
    r1 = roamer(hb, ROAM1, freq=OTHER)
    r1.roaming_interrupt_rx = 'as_needed'
    r1.set_slot_stream(2, StreamState(repeater_id=rid(ROAM1), rf_src=sid(1), dst_id=sid(2), slot=2, start_time=0,
                                      last_seen=0, stream_id=b'\x07' * 4, is_assumed=True, is_unit_call=True))
    heard_at(hb, SIMPLEX)
    with patch.dict(hblink.CONFIG, CHANNELS):
        assert picked(hb, b'\x01' * 4) is None


def two_roamers_and_a_call(hb):
    r1, r2 = roamer(hb, ROAM1, freq=OTHER), roamer(hb, ROAM2, freq=OTHER)
    r2.roaming_priority = 200
    heard_at(hb, SIMPLEX)
    with patch.dict(hblink.CONFIG, CHANNELS):
        stream = start_call(hb)
    assert stream.target_repeaters == {rid(ROAM1)} and not dmrt(r2)
    return r1, r2, stream


def test_a_refusal_hands_the_call_to_the_next_roamer_with_what_was_sent():
    hb = make_hb()
    r1, r2, stream = two_roamers_and_a_call(hb)
    sent_to_r1 = [p for p in r1.sent if p[:4] == b'DMRD']
    with patch.dict(hblink.CONFIG, CHANNELS):
        hb._handle_roaming_ack(rid(ROAM1), dmrk(b'\xaa\xbb\xcc\xdd', 4))         # transceiver busy
    assert stream.target_repeaters == {rid(ROAM2)} and stream.target_slots[rid(ROAM2)] == 2
    assert len(dmrt(r2)) == 1 and [p for p in r2.sent if p[:4] == b'DMRD'] == sent_to_r1
    assert r1.get_slot_stream(2) is None and r2.get_slot_stream(2).is_assumed
    assert not [e for e in hb._events.emitted if e[0] == 'unit_call_unroutable']
    hb._handle_roaming_ack(rid(ROAM2), dmrk(b'\xaa\xbb\xcc\xdd', 0, peer_id=ROAM2))
    assert not hb._roaming_calls


def test_a_busy_channel_ends_it_there_and_a_radio_error_benches_the_roamer():
    hb = make_hb()
    r1, r2, stream = two_roamers_and_a_call(hb)
    with patch.dict(hblink.CONFIG, CHANNELS):
        hb._handle_roaming_ack(rid(ROAM1), dmrk(b'\xaa\xbb\xcc\xdd', 1))         # channel busy
    assert stream.target_repeaters == set() and not dmrt(r2)
    assert [e[1]['reason'] for e in hb._events.emitted if e[0] == 'unit_call_unroutable'] == ['channel busy']

    hb = make_hb()
    r1, r2, stream = two_roamers_and_a_call(hb)
    with patch.dict(hblink.CONFIG, CHANNELS):
        hb._handle_roaming_ack(rid(ROAM1), dmrk(b'\xaa\xbb\xcc\xdd', 3))         # radio error
        assert stream.target_repeaters == {rid(ROAM2)}
        hb._handle_roaming_ack(rid(ROAM2), dmrk(b'\xaa\xbb\xcc\xdd', 0, peer_id=ROAM2))
        assert picked(hb, b'\x09' * 4) != ROAM1                              # benched for a while


def test_no_answer_moves_the_call_on_and_the_last_roamer_failing_drops_it():
    hb = make_hb()
    r1, r2, stream = two_roamers_and_a_call(hb)
    with patch.dict(hblink.CONFIG, CHANNELS):
        hb._roaming_calls[b'\xaa\xbb\xcc\xdd'].sent_at -= 10
        hb._check_roaming_acks()
        assert stream.target_repeaters == {rid(ROAM2)} and r1.roaming_cooldown_until > 0
        hb._handle_roaming_ack(rid(ROAM2), dmrk(b'\xaa\xbb\xcc\xdd', 4, peer_id=ROAM2))
    assert stream.target_repeaters == set() and not hb._roaming_calls
    assert [e[1]['reason'] for e in hb._events.emitted if e[0] == 'unit_call_unroutable'] == ['transceiver busy']
    hb._handle_roaming_ack(rid(ROAM1), dmrk(b'\xaa\xbb\xcc\xdd', 0))              # a late answer: ignored
    assert stream.target_repeaters == set()
