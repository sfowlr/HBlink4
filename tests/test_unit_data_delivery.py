"""
Unit data delivery (unit_data.py): what became of each unit data packet.

- reported like a unit call (routed / failed, `is_data`), under its stream id
- a confirmed packet (DPF 3, response requested) then gets the outcome of the
  radio's response packet (DPF 1), matched by src/dst swapped: delivered
  (ACK), nacked (NACK with the reason, or selective ACK with the blocks
  missing), or no_response within global.unit_data_response_timeout
- the response itself is forwarded back to the sender, not reported
- global.unit_data_retry: HBlink4 sends the packet again (a new stream,
  routed afresh) on no_response / NACK worth it / selective ACK / a
  retryable failure, then reports the final outcome
- a busy destination isn't a failed attempt: the packet waits for room (up to
  busy_wait_s) and goes as soon as the slot is free
"""
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hblink4 import hblink
from hblink4.models import StreamState
from hblink4.unit_data import parse_response, sack_missing

from test_unit_call_routing import MODEM1, RADIO, heard, rid
from test_unit_data_forwarding import GATEWAY_ID, GATEWAY_PEER, bptc_payload, dmrd, feed, gateway_hb
from test_unit_call_status import FakeMqtt

STREAM = 0x5EED0001
TOPIC = 'hblink4/unit_call'
BASE = {'forward_unit_data': True, 'stream_hang_time': 3.0, 'unit_data_response_timeout': 5.0,
        'external_last_heard': {'status_topic': TOPIC}}
RETRY = {**BASE, 'unit_data_retry': {'enabled': True, 'attempts': 2, 'wait_s': 1.0}}


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


def confirmed12(src, dst, blocks, ns=3, response=True):
    return (bytes([(0x40 if response else 0) | 3, 4 << 4]) + dst.to_bytes(3, 'big') + src.to_bytes(3, 'big')
            + bytes([0x80 | blocks, ns << 4 | 8, 0, 0]))


def unconfirmed12(src, dst, blocks):
    return bytes([2, 4 << 4]) + dst.to_bytes(3, 'big') + src.to_bytes(3, 'big') + bytes([0x80 | blocks, 0, 0, 0])


def response12(src, dst, cls, typ, status, blocks=0):
    return (bytes([1, 4 << 4]) + dst.to_bytes(3, 'big') + src.to_bytes(3, 'big')
            + bytes([blocks, cls << 6 | typ << 3 | status, 0, 0]))


def packet(header, src=GATEWAY_ID, dst=RADIO, peer_id=GATEWAY_PEER, slot=1, blocks=3, stream=STREAM):
    """The bot's way: two preamble CSBKs, the header and its blocks, one stream id."""
    return ([dmrd(src, dst, peer_id, slot, 3, stream), dmrd(src, dst, peer_id, slot, 3, stream),
             dmrd(src, dst, peer_id, slot, 6, stream, bptc_payload(header))]
            + [dmrd(src, dst, peer_id, slot, 7, stream, bptc_payload(bytes([n << 1]) + bytes(11)))
               for n in range(blocks)])


def respond(hb, cls, typ, status=3, flags=None, stream=0x77):
    """The radio's response, heard on MODEM1."""
    pkts = [dmrd(RADIO, GATEWAY_ID, MODEM1, 2, 6, stream,
                 bptc_payload(response12(RADIO, GATEWAY_ID, cls, typ, status, 1 if flags else 0)))]
    if flags is not None:
        pkts.append(dmrd(RADIO, GATEWAY_ID, MODEM1, 2, 7, stream, bptc_payload(flags + bytes(4))))
    feed(hb, MODEM1, pkts)
    return pkts


def rig():
    hb = gateway_hb()
    hb._external_last_heard = FakeMqtt()
    heard(hb, RADIO, MODEM1, 2)
    return hb


def outcomes(hb):
    return [(d['status'], d.get('reason'), d.get('attempt')) for kind, d in hb._events.emitted
            if kind == 'unit_call_status' and d['stream_id'] == f'{STREAM:08x}']


def last(hb):
    return [d for kind, d in hb._events.emitted if kind == 'unit_call_status'][-1]


def test_parse_response_and_flags():
    r = parse_response(response12(RADIO, GATEWAY_ID, 0, 1, 5))
    assert (r.outcome, r.kind, r.status, r.reason) == ('delivered', 'ack', 5, None)
    n = parse_response(response12(RADIO, GATEWAY_ID, 1, 4, 0))
    assert (n.outcome, n.reason, n.worth_retrying) == ('nacked', 'undeliverable', False)
    assert parse_response(response12(RADIO, GATEWAY_ID, 1, 1, 0)).worth_retrying
    assert parse_response(unconfirmed12(1, 2, 3)) is None
    assert sack_missing(bytes([0xFE, 0xFD]) + b'\xff' * 6, blocks=12) == [0, 9]


def test_ack_is_delivered_and_goes_back_to_the_sender():
    hb, clock = rig(), Clock()
    with patch.dict(hblink.CONFIG, {'global': BASE}), patch.object(hblink, 'time', clock):
        feed(hb, GATEWAY_PEER, packet(confirmed12(GATEWAY_ID, RADIO, 3)))
        assert outcomes(hb) == [('routed', None, None)]
        assert len(hb._repeaters[rid(MODEM1)].sent) == 6
        clock.t += 1
        resp = respond(hb, 0, 1, status=3)
    assert outcomes(hb) == [('routed', None, None), ('delivered', None, None)]
    d = last(hb)
    assert (d['is_data'], d['response'], d['ns'], d['src_id'], d['dst_id']) == (True, 'ack', 3, GATEWAY_ID, RADIO)
    assert hb._repeaters[rid(GATEWAY_PEER)].sent[-1][20:53] == resp[0][20:53]       # forwarded back
    topics = {t for t, _ in hb._external_last_heard.published}
    assert topics == {f'{TOPIC}/{GATEWAY_ID}'}                                       # the response: not reported


def test_nack_and_selective_ack_are_nacked_with_why():
    hb, clock = rig(), Clock()
    with patch.dict(hblink.CONFIG, {'global': BASE}), patch.object(hblink, 'time', clock):
        feed(hb, GATEWAY_PEER, packet(confirmed12(GATEWAY_ID, RADIO, 3)))
        respond(hb, 1, 1)
    assert outcomes(hb)[-1] == ('nacked', 'packet CRC failed', None) and last(hb)['response'] == 'nack'

    hb = rig()
    with patch.dict(hblink.CONFIG, {'global': BASE}), patch.object(hblink, 'time', clock):
        feed(hb, GATEWAY_PEER, packet(confirmed12(GATEWAY_ID, RADIO, 5), blocks=5))
        respond(hb, 2, 0, flags=bytes([0xF5]) + b'\xff' * 7)                         # blocks 1 and 3
    assert outcomes(hb)[-1] == ('nacked', 'selective retry', None)
    assert (last(hb)['response'], last(hb)['missing']) == ('sack', [1, 3])


def test_no_response_within_the_timeout():
    hb, clock = rig(), Clock()
    with patch.dict(hblink.CONFIG, {'global': BASE}), patch.object(hblink, 'time', clock):
        feed(hb, GATEWAY_PEER, packet(confirmed12(GATEWAY_ID, RADIO, 3)))
        clock.t += 4.9
        hb._check_unit_data()
        assert outcomes(hb) == [('routed', None, None)]
        clock.t += 0.2
        hb._check_unit_data()
        respond(hb, 0, 1)                                                            # too late: ignored
    assert outcomes(hb) == [('routed', None, None), ('no_response', 'no response in 5 s', None)]


def test_unconfirmed_packets_end_at_routed():
    hb, clock = rig(), Clock()
    with patch.dict(hblink.CONFIG, {'global': BASE}), patch.object(hblink, 'time', clock):
        feed(hb, GATEWAY_PEER, packet(unconfirmed12(GATEWAY_ID, RADIO, 3)))
        clock.t += 30
        hb._check_unit_data()
        respond(hb, 0, 1)
    assert outcomes(hb) == [('routed', None, None)]


def test_unroutable_and_not_forwarded_fail():
    hb = gateway_hb()
    hb._external_last_heard = FakeMqtt()
    with patch.dict(hblink.CONFIG, {'global': BASE}):
        feed(hb, GATEWAY_PEER, packet(confirmed12(GATEWAY_ID, RADIO, 3)))
    assert outcomes(hb) == [('failed', 'no route', None)] and last(hb)['is_data']

    hb = rig()
    with patch.dict(hblink.CONFIG, {'global': {**BASE, 'forward_unit_data': False}}):
        feed(hb, GATEWAY_PEER, packet(confirmed12(GATEWAY_ID, RADIO, 3)))
    assert outcomes(hb) == [('failed', 'unit data not forwarded', None)]


def test_a_response_between_other_radios_matches_nothing():
    hb, clock = rig(), Clock()
    with patch.dict(hblink.CONFIG, {'global': BASE}), patch.object(hblink, 'time', clock):
        feed(hb, GATEWAY_PEER, packet(confirmed12(GATEWAY_ID, RADIO, 3)))
        feed(hb, MODEM1, [dmrd(RADIO, 1234, MODEM1, 2, 6, 0x99, bptc_payload(response12(RADIO, 1234, 0, 1, 3)))])
    assert outcomes(hb) == [('routed', None, None)]


def test_retry_sends_the_packet_again_then_reports_the_final_outcome():
    hb, clock = rig(), Clock()
    modem = hb._repeaters[rid(MODEM1)]
    with patch.dict(hblink.CONFIG, {'global': RETRY}), patch.object(hblink, 'time', clock):
        sent = packet(confirmed12(GATEWAY_ID, RADIO, 3))
        feed(hb, GATEWAY_PEER, sent)
        first = list(modem.sent)
        clock.t += 5.1
        hb._check_unit_data()                                    # no response: try again in 1 s
        assert outcomes(hb) == [('routed', None, None)] and len(modem.sent) == 6
        clock.t += 1.0
        hb._check_unit_data()
        again = modem.sent[6:]
        assert len(again) == 6 and [p[20:53] for p in again] == [p[20:53] for p in first]
        assert len({p[16:20] for p in again}) == 1 and again[0][16:20] != first[0][16:20]
        assert all(p[15] & 0x80 for p in again)                  # the radio's slot, TS2
        assert outcomes(hb)[-1] == ('routed', None, 2)
        respond(hb, 0, 1)
    assert outcomes(hb)[-1] == ('delivered', None, 2)


def test_retry_gives_up_after_its_attempts():
    hb, clock = rig(), Clock()
    with patch.dict(hblink.CONFIG, {'global': RETRY}), patch.object(hblink, 'time', clock):
        feed(hb, GATEWAY_PEER, packet(confirmed12(GATEWAY_ID, RADIO, 3)))
        for _ in range(10):
            clock.t += 3
            hb._check_unit_data()
    assert outcomes(hb)[-1] == ('no_response', 'no response in 5 s', 3)
    assert len(hb._repeaters[rid(MODEM1)].sent) == 18


def test_retry_after_no_route_once_the_radio_is_heard():
    hb, clock = gateway_hb(), Clock()
    hb._external_last_heard = FakeMqtt()
    with patch.dict(hblink.CONFIG, {'global': RETRY}), patch.object(hblink, 'time', clock):
        feed(hb, GATEWAY_PEER, packet(unconfirmed12(GATEWAY_ID, RADIO, 2), blocks=2))
        assert outcomes(hb) == []                                # not final yet: it'll be tried again
        heard(hb, RADIO, MODEM1, 2)
        clock.t += 1.0
        hb._check_unit_data()
    assert outcomes(hb) == [('routed', None, 2)]
    assert len(hb._repeaters[rid(MODEM1)].sent) == 5


def test_retry_does_not_retry_a_final_nack():
    hb, clock = rig(), Clock()
    with patch.dict(hblink.CONFIG, {'global': RETRY}), patch.object(hblink, 'time', clock):
        feed(hb, GATEWAY_PEER, packet(confirmed12(GATEWAY_ID, RADIO, 3)))
        respond(hb, 1, 4)                                        # undeliverable
        clock.t += 5
        hb._check_unit_data()
    assert outcomes(hb)[-1] == ('nacked', 'undeliverable', None) and len(hb._repeaters[rid(MODEM1)].sent) == 6


def test_retry_is_off_by_default_and_keeps_nothing():
    hb, clock = rig(), Clock()
    with patch.dict(hblink.CONFIG, {'global': BASE}), patch.object(hblink, 'time', clock):
        feed(hb, GATEWAY_PEER, packet(confirmed12(GATEWAY_ID, RADIO, 3)))
        (rec,) = list(hb._unit_data.values())
        assert rec.packets is None and rec.confirmed and rec.ns == 3 and rec.blocks == 3
        clock.t += 6
        hb._check_unit_data()
    assert len(hb._repeaters[rid(MODEM1)].sent) == 6


# ── a busy destination ───────────────────────────────────────────────────────

ONCE = {**BASE, 'unit_data_retry': {'enabled': True, 'attempts': 1, 'wait_s': 10.0, 'busy_wait_s': 120.0}}


def busy(hb, clock, src=3333, stream=b'\xbb\xbb\xbb\xbb'):
    """Another radio's group call on MODEM1 TS2, the slot our radio was heard on."""
    s = StreamState(repeater_id=rid(MODEM1), rf_src=src.to_bytes(3, 'big'), dst_id=(9).to_bytes(3, 'big'),
                    slot=2, start_time=clock.t, last_seen=clock.t, stream_id=stream, call_type='group')
    hb._repeaters[rid(MODEM1)].set_slot_stream(2, s)
    return s


def poll(hb, clock, seconds, step=0.5):
    for _ in range(int(seconds / step)):
        clock.t += step
        hb._check_unit_data()


def test_a_busy_slot_waits_for_room_without_using_an_attempt():
    hb, clock = rig(), Clock()
    modem = hb._repeaters[rid(MODEM1)]
    with patch.dict(hblink.CONFIG, {'global': ONCE}), patch.object(hblink, 'time', clock):
        call = busy(hb, clock)
        feed(hb, GATEWAY_PEER, packet(confirmed12(GATEWAY_ID, RADIO, 3)))
        poll(hb, clock, 30)                                      # a long call: nothing sent, nothing final
        assert modem.sent == [] and outcomes(hb) == []
        call.ended, call.end_time = True, clock.t                # it ends; then its hang time (3 s)
        poll(hb, clock, 2.5)
        assert modem.sent == []
        poll(hb, clock, 1.0)
        assert len(modem.sent) == 6 and outcomes(hb) == [('routed', None, None)]   # still the first attempt
        clock.t += 5.1
        hb._check_unit_data()                                    # no response: the one retry, 10 s on
        poll(hb, clock, 10)
        assert len(modem.sent) == 12 and outcomes(hb)[-1] == ('routed', None, 2)
        respond(hb, 0, 1)
    assert outcomes(hb)[-1] == ('delivered', None, 2)


def test_a_retry_that_finds_the_slot_busy_waits_too():
    hb, clock = rig(), Clock()
    modem = hb._repeaters[rid(MODEM1)]
    with patch.dict(hblink.CONFIG, {'global': ONCE}), patch.object(hblink, 'time', clock):
        feed(hb, GATEWAY_PEER, packet(confirmed12(GATEWAY_ID, RADIO, 3)))
        clock.t += 5.1
        hb._check_unit_data()                                    # no response
        call = busy(hb, clock)                                   # and now the radio's slot is taken
        poll(hb, clock, 40)
        assert len(modem.sent) == 6 and outcomes(hb) == [('routed', None, None)]
        call.ended, call.end_time = True, clock.t
        poll(hb, clock, 3.5)
        assert len(modem.sent) == 12 and outcomes(hb)[-1] == ('routed', None, 2)


def test_a_slot_busy_past_busy_wait_s_fails():
    hb, clock = rig(), Clock()
    with patch.dict(hblink.CONFIG, {'global': ONCE}), patch.object(hblink, 'time', clock):
        busy(hb, clock)
        feed(hb, GATEWAY_PEER, packet(confirmed12(GATEWAY_ID, RADIO, 3)))
        poll(hb, clock, 119)
        assert outcomes(hb) == []
        poll(hb, clock, 2)
    assert outcomes(hb) == [('failed', 'slot busy for 120 s', None)]
    assert hb._repeaters[rid(MODEM1)].sent == []


def test_a_newer_packet_replaces_one_waiting_for_room():
    hb, clock = rig(), Clock()
    modem = hb._repeaters[rid(MODEM1)]
    with patch.dict(hblink.CONFIG, {'global': ONCE}), patch.object(hblink, 'time', clock):
        call = busy(hb, clock)
        feed(hb, GATEWAY_PEER, packet(confirmed12(GATEWAY_ID, RADIO, 3)))
        poll(hb, clock, 4)
        feed(hb, GATEWAY_PEER, packet(confirmed12(GATEWAY_ID, RADIO, 3, ns=4), stream=0x5EED0002))  # the sender's own retry
        assert outcomes(hb) == [('no_response', 'superseded by a newer packet', None)]
        call.ended, call.end_time = True, clock.t
        poll(hb, clock, 3.5)
    assert len(modem.sent) == 6                                  # only the newer one goes out


def test_slot_busy_is_the_route_reason():
    hb, clock = rig(), Clock()
    with patch.dict(hblink.CONFIG, {'global': BASE}), patch.object(hblink, 'time', clock):
        busy(hb, clock)
        feed(hb, GATEWAY_PEER, packet(confirmed12(GATEWAY_ID, RADIO, 3)))
    assert outcomes(hb) == [('failed', 'slot busy', None)]


# ── regressions seen on the sandbox, 2026-10-08 ──────────────────────────────

def test_static_subscriber_pins_all_survive_startup():
    pins = {'9990199': 9990200, '9990': 9990202, '16776961': 9990210, '16776962': 9990210}
    with patch.dict(hblink.CONFIG, {'global': {**BASE, 'static_subscribers': pins}}):
        hb = hblink.HBProtocol()
    for radio_id, peer_id in pins.items():
        assert hb._user_cache.lookup(int(radio_id)).repeater_id == peer_id


def test_a_one_packet_response_ends_its_assumed_stream_on_the_senders_peer():
    """A response header (no blocks) is a whole data stream in one packet. Its assumed
    stream on the bot's peer was never ended (the terminator ended the slot's previous
    stream), so the slot read busy for stream_timeout plus hang time and a radio's call
    to the bot 5 s later got [no route]."""
    hb, clock = rig(), Clock()
    with patch.dict(hblink.CONFIG, {'global': BASE}), patch.object(hblink, 'time', clock):
        feed(hb, GATEWAY_PEER, packet(confirmed12(GATEWAY_ID, RADIO, 3)))
        clock.t += 0.5
        respond(hb, 0, 1)
        bot_slot = hb._repeaters[rid(GATEWAY_PEER)].get_slot_stream(2)
        assert bot_slot.is_assumed and bot_slot.ended


def test_a_call_to_the_pinned_bot_routes_after_a_response_and_a_dmrc_move():
    from hblink4.access_control import RepeaterMatcher
    from test_unit_call_routing import CONSOLE_PEER, CONSOLE_RADIO, sid
    BOT_PEER, BOT = 9990200, 9990199
    hb, clock = rig(), Clock()
    hb.transport = type('T', (), {'sendto': lambda self, data, addr: None})()
    hb._matcher = RepeaterMatcher({'repeater_configurations': {'patterns': [
        {'name': 'bot', 'description': '', 'match': {'ids': [BOT_PEER]},
         'config': {'passphrase': 'x', 'default_unit_calls': True}}]}})
    body = ('%-8.8s%09u%09u%02u%02u%c%-40.40s%-40.40s' % ('RDBOT', 0, 0, 1, 1, '3', 'v', 's')).encode()
    dmrc = b'DMRC' + rid(BOT_PEER) + body
    from hblink4 import models
    with patch.dict(hblink.CONFIG, {'global': {**BASE, 'static_subscribers': {str(BOT): BOT_PEER}}}), \
            patch.object(hblink, 'time', clock), patch.object(models, 'time', clock):
        hb._user_cache.pin(BOT, BOT_PEER)
        hb._handle_dmrc(rid(BOT_PEER), dmrc, ('127.0.0.1', 33256))
        bot = hb._repeaters[rid(BOT_PEER)]
        bot.sent = []
        bot.send = bot.sent.append
        feed(hb, BOT_PEER, packet(confirmed12(BOT, RADIO, 3), src=BOT, peer_id=BOT_PEER))
        clock.t += 0.5
        feed(hb, MODEM1, [dmrd(RADIO, BOT, MODEM1, 2, 6, 0x77, bptc_payload(response12(RADIO, BOT, 0, 1, 3)))])
        assert bot.sent                                           # the response reached the bot
        for _ in range(3):
            clock.t += 1
            hb._check_stream_timeouts()
        hb._handle_dmrc(rid(BOT_PEER), dmrc, ('127.0.0.1', 57246))   # the bot restarted: a new port
        clock.t += 1                                              # 4 s after the response, past hang time
        console = hb._repeaters[rid(CONSOLE_PEER)]
        assert hb._handle_unit_stream_start(console, sid(CONSOLE_RADIO), sid(BOT), 2, b'\x87\xb8\x72\x9e')
        assert console.get_slot_stream(2).target_repeaters == {rid(BOT_PEER)}
