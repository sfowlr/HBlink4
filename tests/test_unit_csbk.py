"""
A single unit CSBK (a radio check, call alert …: one burst, no header, no
terminator), with global.forward_unit_data: routed to where the radio was last
heard and ended there and then, so the radio's answering CSBK can come
straight back to the sender's peer; reported routed / failed like any unit
data, never left waiting for a data header or a response.
"""
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hblink4 import hblink
from hblink4.lc import CSBK_CRC_MASK, crc16_ccitt

from test_unit_call_routing import MODEM1, RADIO, heard, rid
from test_unit_data_forwarding import GATEWAY_ID, GATEWAY_PEER, bptc_payload, dmrd, feed, gateway_hb
from test_unit_call_status import FakeMqtt
from test_unit_data_delivery import BASE, Clock, RETRY

REQ, ACK = 0x5EED0101, 0x5EED0202


def radio_check12(src, dst, answer=False):
    """Motorola radio check (opcode 0x24, FID 0x10): the request has octet 3 = 0x80, target then source;
    the radio's answer has octet 3 = 0, source then target (MMDVMHost DMRCSBK.cpp)."""
    if answer:
        head = bytes([0xA4, 0x10, 0, 0]) + src.to_bytes(3, 'big') + dst.to_bytes(3, 'big')
    else:
        head = bytes([0xA4, 0x10, 0, 0x80]) + dst.to_bytes(3, 'big') + src.to_bytes(3, 'big')
    return head + (crc16_ccitt(head) ^ CSBK_CRC_MASK).to_bytes(2, 'big')


def rig():
    hb = gateway_hb()
    hb._external_last_heard = FakeMqtt()
    heard(hb, RADIO, MODEM1, 2)
    return hb


def statuses(hb, stream):
    return [(d['status'], d.get('reason')) for kind, d in hb._events.emitted
            if kind == 'unit_call_status' and d['stream_id'] == f'{stream:08x}']


def check_and_answer(hb, clock, gap=0.4):
    feed(hb, GATEWAY_PEER, [dmrd(GATEWAY_ID, RADIO, GATEWAY_PEER, 1, 3, REQ,
                                 bptc_payload(radio_check12(GATEWAY_ID, RADIO)))])
    clock.t += gap
    ack = dmrd(RADIO, GATEWAY_ID, MODEM1, 2, 3, ACK, bptc_payload(radio_check12(RADIO, GATEWAY_ID, answer=True)))
    feed(hb, MODEM1, [ack])
    return ack


def test_radio_check_goes_to_the_radio_and_its_answer_comes_back():
    hb, clock = rig(), Clock()
    with patch.dict(hblink.CONFIG, {'global': BASE}), patch.object(hblink, 'time', clock):
        ack = check_and_answer(hb, clock)
        modem, gateway = hb._repeaters[rid(MODEM1)], hb._repeaters[rid(GATEWAY_PEER)]
        assert len(modem.sent) == 1 and modem.sent[0][15] & 0x80                      # on the radio's TS2
        assert [p[20:53] for p in gateway.sent] == [ack[20:53]]                       # the answer, back
        assert statuses(hb, REQ) == [('routed', None)]
        assert statuses(hb, ACK) == [('routed', None)]
        # both ended at once: nothing holds either slot past hang time rules
        assert hb._repeaters[rid(GATEWAY_PEER)].get_slot_stream(1).ended
        assert modem.get_slot_stream(2).ended
        clock.t += 10
        hb._check_unit_data()
    assert statuses(hb, REQ) == [('routed', None)]                                    # no no_response, no failed


def test_a_second_check_right_after_the_first_goes_too():
    hb, clock = rig(), Clock()
    with patch.dict(hblink.CONFIG, {'global': BASE}), patch.object(hblink, 'time', clock):
        check_and_answer(hb, clock)
        clock.t += 0.5
        feed(hb, GATEWAY_PEER, [dmrd(GATEWAY_ID, RADIO, GATEWAY_PEER, 1, 3, REQ + 7,
                                     bptc_payload(radio_check12(GATEWAY_ID, RADIO)))])
    assert len(hb._repeaters[rid(MODEM1)].sent) == 2
    assert statuses(hb, REQ + 7) == [('routed', None)]


def test_radio_check_to_an_unknown_radio_fails():
    hb, clock = gateway_hb(), Clock()
    hb._external_last_heard = FakeMqtt()
    with patch.dict(hblink.CONFIG, {'global': BASE}), patch.object(hblink, 'time', clock):
        feed(hb, GATEWAY_PEER, [dmrd(GATEWAY_ID, RADIO, GATEWAY_PEER, 1, 3, REQ,
                                     bptc_payload(radio_check12(GATEWAY_ID, RADIO)))])
    assert statuses(hb, REQ) == [('failed', 'no route')]


def test_radio_check_with_unit_data_retry_is_not_kept_waiting():
    hb, clock = rig(), Clock()
    with patch.dict(hblink.CONFIG, {'global': RETRY}), patch.object(hblink, 'time', clock):
        check_and_answer(hb, clock)
        for _ in range(20):
            clock.t += 1
            hb._check_unit_data()
    assert statuses(hb, REQ) == [('routed', None)]
    assert len(hb._repeaters[rid(MODEM1)].sent) == 1                                 # not sent again


def test_a_preamble_or_a_bad_crc_is_not_a_single_csbk():
    from hblink4.lc import is_single_csbk
    good = radio_check12(GATEWAY_ID, RADIO)
    assert is_single_csbk(bptc_payload(good))
    assert not is_single_csbk(bptc_payload(good[:10] + bytes(2)))
    pre = bytes([0xBD, 0, 0x80, 4]) + RADIO.to_bytes(3, 'big') + GATEWAY_ID.to_bytes(3, 'big')
    assert not is_single_csbk(bptc_payload(pre + (crc16_ccitt(pre) ^ CSBK_CRC_MASK).to_bytes(2, 'big')))


def test_preamble_csbks_still_wait_for_their_header():
    from test_unit_data_delivery import packet, confirmed12
    hb, clock = rig(), Clock()
    with patch.dict(hblink.CONFIG, {'global': BASE}), patch.object(hblink, 'time', clock):
        pkts = packet(confirmed12(GATEWAY_ID, RADIO, 3))
        feed(hb, GATEWAY_PEER, pkts[:2])
        assert not hb._repeaters[rid(GATEWAY_PEER)].get_slot_stream(1).ended
        feed(hb, GATEWAY_PEER, pkts[2:])
    assert len(hb._repeaters[rid(MODEM1)].sent) == 6


def test_radio_check_through_a_roaming_transceiver_and_back():
    from test_roaming import CHANNELS, ROAM1, SIMPLEX, dmrk, heard_at, roamer
    hb, clock = gateway_hb(), Clock()
    hb._external_last_heard = FakeMqtt()
    r = roamer(hb, ROAM1)
    heard_at(hb, SIMPLEX)
    cfg = {'global': {**BASE, **CHANNELS['global']}}
    with patch.dict(hblink.CONFIG, cfg), patch.object(hblink, 'time', clock):
        feed(hb, GATEWAY_PEER, [dmrd(GATEWAY_ID, RADIO, GATEWAY_PEER, 1, 3, REQ,
                                     bptc_payload(radio_check12(GATEWAY_ID, RADIO)))])
        sent = [p for p in r.sent if p[:4] == b'DMRD']
        assert len(sent) == 1 and sent[0][15] & 0x80                                    # on TS2
        hb._handle_roaming_ack(rid(ROAM1), dmrk(REQ.to_bytes(4, 'big'), 0))
        assert statuses(hb, REQ) == [('on_air', None)]
        clock.t += 0.3
        ack = dmrd(RADIO, GATEWAY_ID, ROAM1, 2, 3, ACK, bptc_payload(radio_check12(RADIO, GATEWAY_ID, answer=True)))
        feed(hb, ROAM1, [ack])
    assert [p[20:53] for p in hb._repeaters[rid(GATEWAY_PEER)].sent] == [ack[20:53]]
