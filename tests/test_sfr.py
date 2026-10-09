"""
Single frequency repeaters (pattern `sfr_slot`): one frequency, radios heard on
one slot, repeated by the repeater itself on the other.

- whatever is sent to it goes out on its outbound slot: unit calls to a radio
  heard on its inbound slot, and group calls whatever their network slot
- its outbound slot is busy while it's repeating a call heard on the inbound one
- a receiver on its frequency hearing the repeat (outbound slot) is hearing an
  echo, not the radio: not routed, and not where the radio is
"""
import os
import sys
from time import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hblink4.access_control import RepeaterConfig
from test_unit_call_routing import CONSOLE_PEER, CONSOLE_RADIO, RADIO, make_hb, peer, rid, route, sid

SFR, SFR_SDR = 311050, 3128050
FREQ = 462_550_000


def sfr_hb():
    hb = make_hb()
    r = peer(hb, SFR, freq=FREQ)
    r.tx_freq = r.rx_freq                    # one frequency
    r.sfr_slot = 2
    sdr = peer(hb, SFR_SDR, freq=FREQ, tx=False, unit=False)
    sdr.tx_freq = sdr.rx_freq
    return hb, r, sdr


def dmrd(src, dst, slot, stream_id, peer_id, group=False):
    pkt = bytearray(55)
    pkt[0:4] = b'DMRD'
    pkt[5:8] = sid(src)
    pkt[8:11] = sid(dst)
    pkt[11:15] = rid(peer_id)
    pkt[15] = (0x80 if slot == 2 else 0) | (0 if group else 0x40) | 0x10
    pkt[16:20] = stream_id
    return bytes(pkt)


def test_pattern_setting():
    assert RepeaterConfig(passphrase='x', sfr_slot=2).sfr_slot == 2
    with pytest.raises(ValueError):
        RepeaterConfig(passphrase='x', sfr_slot=3)


def test_a_radio_heard_on_the_inbound_slot_is_called_on_the_outbound_one():
    hb, r, _ = sfr_hb()
    assert hb._handle_unit_stream_start(r, sid(RADIO), sid(CONSOLE_RADIO), 1, b'\x01' * 4)
    r.get_slot_stream(1).ended = True
    r.get_slot_stream(1).end_time = time() - 60     # long over
    assert route(hb, slot=1) == ({rid(SFR)}, False, {rid(SFR): 2})

    stream_id = b'\x02' * 4
    console = hb._repeaters[rid(CONSOLE_PEER)]
    assert hb._handle_unit_stream_start(console, sid(CONSOLE_RADIO), sid(RADIO), 1, stream_id)
    hb._forward_stream(dmrd(CONSOLE_RADIO, RADIO, 1, stream_id, CONSOLE_PEER), rid(CONSOLE_PEER), 1,
                       sid(CONSOLE_RADIO), sid(RADIO), stream_id)
    assert [p[15] & 0x80 for p in r.sent] == [0x80]   # TS2


def test_a_radio_heard_by_an_sdr_on_its_frequency_is_called_on_the_outbound_slot():
    hb, r, sdr = sfr_hb()
    hb._user_cache.update(radio_id=RADIO, repeater_id=SFR_SDR, callsign='', slot=1, talkgroup=9,
                          **hb._peer_channel(SFR_SDR))
    assert route(hb, slot=1) == ({rid(SFR)}, False, {rid(SFR): 2})


def test_the_outbound_slot_is_busy_while_it_repeats():
    hb, r, _ = sfr_hb()
    assert hb._handle_unit_stream_start(r, sid(RADIO), sid(9999), 1, b'\x03' * 4)
    assert hb._is_slot_busy(rid(SFR), 2, b'\x04' * 4, sid(CONSOLE_RADIO), sid(1234), is_unit_call=True)
    assert hb._is_slot_busy(rid(SFR), 2, b'\x04' * 4, sid(9999), sid(RADIO), is_unit_call=True)
    # Once it's over, the same pair answering in the hang time isn't kept off.
    r.get_slot_stream(1).ended = True
    r.get_slot_stream(1).end_time = time()
    assert hb._is_slot_busy(rid(SFR), 2, b'\x04' * 4, sid(CONSOLE_RADIO), sid(1234), is_unit_call=True)
    assert not hb._is_slot_busy(rid(SFR), 2, b'\x04' * 4, sid(9999), sid(RADIO), is_unit_call=True)
    # An ordinary repeater's slots are separate.
    r.sfr_slot = None
    assert not hb._is_slot_busy(rid(SFR), 2, b'\x04' * 4, sid(CONSOLE_RADIO), sid(1234), is_unit_call=True)


def test_group_calls_go_out_on_the_outbound_slot():
    hb, r, _ = sfr_hb()
    stream_id = b'\x05' * 4
    targets = hb._calculate_stream_targets(rid(CONSOLE_PEER), 1, sid(9), stream_id, sid(CONSOLE_RADIO))
    assert rid(SFR) in targets
    r.set_slot_stream(1, None)
    hb._is_slot_busy = lambda rid_, slot, *a, **k: (rid_, slot) == (rid(SFR), 2)
    assert rid(SFR) not in hb._calculate_stream_targets(rid(CONSOLE_PEER), 1, sid(9), stream_id,
                                                        sid(CONSOLE_RADIO))


def test_the_repeat_heard_by_a_receiver_is_an_echo():
    hb, r, sdr = sfr_hb()
    assert hb._handle_unit_stream_start(r, sid(RADIO), sid(CONSOLE_RADIO), 1, b'\x06' * 4)
    assert hb._our_rf_echo(sdr, sid(RADIO), sid(CONSOLE_RADIO), 2) == rid(SFR)
    # The radio itself, heard on the inbound slot, is the call.
    assert hb._our_rf_echo(sdr, sid(RADIO), sid(CONSOLE_RADIO), 1) is None
    # Another call on the outbound slot isn't the repeat.
    assert hb._our_rf_echo(sdr, sid(RADIO), sid(4321), 2) is None
    # Nor is it on another frequency.
    sdr.rx_freq = str(FREQ + 25_000).encode()
    assert hb._our_rf_echo(sdr, sid(RADIO), sid(CONSOLE_RADIO), 2) is None


def test_the_repeat_doesnt_move_the_radio_to_the_outbound_slot():
    hb, r, sdr = sfr_hb()
    sdr.unit_calls_enabled = True
    assert hb._handle_unit_stream_start(r, sid(RADIO), sid(CONSOLE_RADIO), 1, b'\x07' * 4)
    assert not hb._handle_unit_stream_start(sdr, sid(RADIO), sid(CONSOLE_RADIO), 2, b'\x08' * 4)
    entry = hb._user_cache.lookup(RADIO)
    assert (entry.repeater_id, entry.slot) == (SFR, 1)
