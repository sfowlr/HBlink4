"""
Unit (private) data calls, with global.forward_unit_data: routed like unit
voice calls, one data transaction (preamble CSBKs, data header, blocks — each
with its own stream id from MMDVMHost) forwarded as one stream, ended when the
header's blocks are all in so a reply can go straight back.
"""
import os
import sys
from unittest.mock import patch

from bitarray import bitarray
from dmr_utils3 import bptc

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hblink4 import hblink
from hblink4.lc import decode_data_header
from test_unit_call_routing import MODEM1, RADIO, SDR1, heard, make_hb, peer, rid, sid

GATEWAY_PEER, GATEWAY_ID = 3129100, 16776961
FORWARD = {'global': {'forward_unit_data': True, 'stream_hang_time': 3.0}}


def bptc_payload(info12: bytes) -> bytes:
    """A 33-byte BPTC(196,96) burst (sync / slot type left zero)."""
    bits = bitarray(endian='big')
    bits.frombytes(info12)
    enc = bptc.interleave_19696(bptc.encode_19696(bits))
    return (enc[0:98] + bitarray('0' * 68) + enc[98:196]).tobytes()


def header12(src, dst, blocks, dpf=2, sap=4):
    return bytes([dpf, sap << 4]) + dst.to_bytes(3, 'big') + src.to_bytes(3, 'big') \
        + bytes([0x80 | blocks, 0, 0, 0])


def dmrd(src, dst, peer_id, slot, dt, stream, payload=b'\0' * 33):
    flags = (0x80 if slot == 2 else 0) | 0x40 | 0x20 | dt      # private, data sync
    return (b'DMRD' + b'\0' + sid(src) + sid(dst) + rid(peer_id) + bytes([flags])
            + stream.to_bytes(4, 'big') + payload + b'\0\0')


def transaction(src, dst, peer_id, slot, blocks=2, first_stream=0x100):
    """Two preamble CSBKs, a data header and its blocks, as MMDVMHost sends
    them: a new stream id for each CSBK and for the header (blocks reuse it)."""
    return ([dmrd(src, dst, peer_id, slot, 3, first_stream),
             dmrd(src, dst, peer_id, slot, 3, first_stream + 1),
             dmrd(src, dst, peer_id, slot, 6, first_stream + 2, bptc_payload(header12(src, dst, blocks)))]
            + [dmrd(src, dst, peer_id, slot, 7, first_stream + 2) for _ in range(blocks)])


def feed(hb, peer_id, packets):
    r = hb._repeaters[rid(peer_id)]
    for p in packets:
        hb._handle_dmr_data(p, (r.ip, r.port))


def gateway_hb():
    hb = make_hb()
    peer(hb, GATEWAY_PEER)
    hb._user_cache.pin(GATEWAY_ID, GATEWAY_PEER)
    return hb


def test_data_header_blocks_to_follow():
    h = decode_data_header(bptc_payload(header12(RADIO, GATEWAY_ID, 5)))
    assert (h['dpf'], h['sap'], h['blocks_to_follow']) == (2, 4, 5)


def test_unit_data_is_not_forwarded_by_default():
    hb = gateway_hb()
    feed(hb, MODEM1, transaction(RADIO, GATEWAY_ID, MODEM1, 2))
    assert hb._repeaters[rid(GATEWAY_PEER)].sent == []


def test_whole_transaction_reaches_the_target_peer():
    hb = gateway_hb()
    packets = transaction(RADIO, GATEWAY_ID, MODEM1, 2, blocks=3)
    with patch.dict(hblink.CONFIG, FORWARD):
        feed(hb, MODEM1, packets)
    assert hb._repeaters[rid(GATEWAY_PEER)].sent == packets     # caller's slot: the pin has none
    source = hb._repeaters[rid(MODEM1)].get_slot_stream(2)
    assert source.ended and source.call_type == 'data'
    assert hb._repeaters[rid(GATEWAY_PEER)].get_slot_stream(2).ended


def test_reply_goes_straight_back_on_the_radios_slot():
    hb = gateway_hb()
    heard(hb, RADIO, MODEM1, 2)
    with patch.dict(hblink.CONFIG, FORWARD):
        feed(hb, MODEM1, transaction(RADIO, GATEWAY_ID, MODEM1, 2))
        reply = transaction(GATEWAY_ID, RADIO, GATEWAY_PEER, 1, blocks=1, first_stream=0x200)[2:]
        feed(hb, GATEWAY_PEER, reply)
        modem = hb._repeaters[rid(MODEM1)]
        assert len(modem.sent) == 2 and all(p[15] & 0x80 for p in modem.sent)   # moved to TS2
        assert [p[20:53] for p in modem.sent] == [p[20:53] for p in reply]
        # and a second reply right after the first
        feed(hb, GATEWAY_PEER, transaction(GATEWAY_ID, RADIO, GATEWAY_PEER, 1, blocks=1,
                                           first_stream=0x300)[2:])
        assert len(modem.sent) == 4


def test_unroutable_unit_data_emits_event_and_sends_nothing():
    hb = gateway_hb()
    with patch.dict(hblink.CONFIG, FORWARD):
        feed(hb, GATEWAY_PEER, transaction(GATEWAY_ID, RADIO, GATEWAY_PEER, 1, blocks=1))
    assert ('unit_call_unroutable', {'repeater_id': GATEWAY_PEER, 'slot': 1, 'src_id': GATEWAY_ID,
                                     'dst_id': RADIO, 'is_data': True}) in hb._events.emitted
    assert all(not r.sent for r in hb._repeaters.values())


def test_receive_only_peer_forwards_unit_data_only_to_a_pinned_id():
    """A radio's reply to a gateway (a TMS ACK right after its data-level ACK) may be heard only by an SDR
    (on air, 2026-10-09): it goes to the pinned gateway. Unit data between radios it hears still doesn't."""
    hb = gateway_hb()
    with patch.dict(hblink.CONFIG, FORWARD):
        packets = transaction(RADIO, GATEWAY_ID, SDR1, 1)
        feed(hb, SDR1, packets)
        assert len(hb._repeaters[rid(GATEWAY_PEER)].sent) == len(packets)       # preambles repeat: all go
        feed(hb, SDR1, transaction(RADIO, 3100999, SDR1, 1, first_stream=0x400))
    assert all(not r.sent for r in hb._repeaters.values() if r.repeater_id != rid(GATEWAY_PEER))
    assert len(hb._repeaters[rid(GATEWAY_PEER)].sent) == len(packets)


def test_the_same_unit_data_heard_by_two_peers_goes_once():
    hb = gateway_hb()
    with patch.dict(hblink.CONFIG, FORWARD):
        feed(hb, MODEM1, transaction(RADIO, GATEWAY_ID, MODEM1, 2))
        n = len(hb._repeaters[rid(GATEWAY_PEER)].sent)
        feed(hb, SDR1, transaction(RADIO, GATEWAY_ID, SDR1, 2, first_stream=0x500))   # the SDR hears it too
    assert n > 0 and len(hb._repeaters[rid(GATEWAY_PEER)].sent) == n
