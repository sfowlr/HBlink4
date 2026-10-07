"""
Unit-call outcomes: what became of each unit call, so its sender can try again.

- 'routed' when forwarded to a fixed peer (or broadcast), 'on_air' when a
  roaming transceiver answers DMRK on air, 'failed' with a reason otherwise
  (no route, channel busy, no roamer free, refused, hang time)
- an event, and on MQTT at {external_last_heard.status_topic}/{src} when set
- each status once per stream
"""
import json
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hblink4 import hblink
from hblink4.models import StreamState

from test_roaming import CHANNELS, ROAM1, SIMPLEX, dmrk, heard_at, roamer, start_call
from test_unit_call_routing import CONSOLE_PEER, CONSOLE_RADIO, MODEM1, RADIO, SDR1, heard, make_hb, rid, sid

STREAM = b'\xaa\xbb\xcc\xdd'
CONFIG = {'global': {**CHANNELS['global'], 'external_last_heard': {'status_topic': 'hblink4/unit_call'}}}


class FakeMqtt:
    def __init__(self):
        self.published = []

    def publish(self, topic, payload, qos=0):
        self.published.append((topic, json.loads(payload)))


def statuses(hb):
    return [(d['stream_id'], d['status'], d.get('reason')) for kind, d in hb._events.emitted
            if kind == 'unit_call_status']


def setup():
    hb = make_hb()
    hb._external_last_heard = FakeMqtt()
    return hb


def test_routed_to_a_fixed_peer():
    hb = setup()
    heard(hb, RADIO, MODEM1, 2)
    with patch.dict(hblink.CONFIG, CONFIG):
        start_call(hb)
    assert statuses(hb) == [(STREAM.hex(), 'routed', None)]
    [(topic, msg)] = hb._external_last_heard.published
    assert topic == f'hblink4/unit_call/{CONSOLE_RADIO}'
    assert (msg['src_id'], msg['dst_id'], msg['status']) == (CONSOLE_RADIO, RADIO, 'routed') and msg['at'] > 0


def test_no_route_fails_at_once():
    hb = setup()
    with patch.dict(hblink.CONFIG, CONFIG):
        start_call(hb)
    assert statuses(hb) == [(STREAM.hex(), 'failed', 'no route')]


def test_a_roamed_call_is_on_air_only_when_the_roamer_says():
    hb = setup()
    r = roamer(hb, ROAM1)
    heard_at(hb, SIMPLEX)
    with patch.dict(hblink.CONFIG, CONFIG):
        start_call(hb)
        assert statuses(hb) == []                                    # waiting for DMRK
        hb._handle_roaming_ack(rid(ROAM1), dmrk(STREAM, 0))
    assert statuses(hb) == [(STREAM.hex(), 'on_air', None)]
    [(_, msg)] = hb._external_last_heard.published
    assert (msg['repeater_id'], msg['freq']) == (ROAM1, SIMPLEX)
    assert r.roaming


def test_a_roamer_refusing_fails_the_call_with_its_reason():
    hb = setup()
    roamer(hb, ROAM1)
    heard_at(hb, SIMPLEX)
    with patch.dict(hblink.CONFIG, CONFIG):
        start_call(hb)
        hb._handle_roaming_ack(rid(ROAM1), dmrk(STREAM, 1))          # channel busy
    assert statuses(hb) == [(STREAM.hex(), 'failed', 'channel busy')]


def test_a_busy_channel_or_no_free_roamer_fails_at_the_start():
    hb = setup()
    heard_at(hb, SIMPLEX)
    with patch.dict(hblink.CONFIG, CONFIG):
        start_call(hb)                                               # no roamer at all
    assert statuses(hb) == [(STREAM.hex(), 'failed', 'no roaming transceiver free')]

    hb = setup()
    roamer(hb, ROAM1)
    sdr = hb._repeaters[rid(SDR1)]
    sdr.rx_freq = str(SIMPLEX).encode()
    sdr.set_slot_stream(1, StreamState(repeater_id=rid(SDR1), rf_src=sid(1), dst_id=sid(2), slot=1,
                                       start_time=0, last_seen=0, stream_id=b'\x09' * 4))
    heard_at(hb, SIMPLEX)
    with patch.dict(hblink.CONFIG, CONFIG):
        start_call(hb)
    assert statuses(hb) == [(STREAM.hex(), 'failed', 'channel busy')]


def test_each_status_once_and_nothing_on_mqtt_without_a_topic():
    hb = setup()
    console = hb._repeaters[rid(CONSOLE_PEER)]
    console.unit_calls_enabled = False
    for _ in range(3):                                               # every packet of a refused stream
        hb._handle_unit_stream_start(console, sid(CONSOLE_RADIO), sid(RADIO), 1, STREAM)
    assert statuses(hb) == [(STREAM.hex(), 'failed', 'unit calls not enabled')]
    assert hb._external_last_heard.published == []                   # no status_topic configured


def test_a_refused_stream_is_logged_once_and_says_why(caplog):
    """2026-10-07: an SDR hearing the roamer send Brian's answer logged ~60 identical
    'UNIT CALL rejected' lines per call."""
    import logging
    hb = setup()
    r = roamer(hb, ROAM1, freq=SIMPLEX)
    r.set_slot_stream(2, StreamState(repeater_id=rid(ROAM1), rf_src=sid(CONSOLE_RADIO), dst_id=sid(RADIO),
                                     slot=2, start_time=0, last_seen=0, stream_id=STREAM, is_assumed=True))
    sdr = hb._repeaters[rid(SDR1)]                   # receive-only, unit calls off, on 461.6875
    with caplog.at_level(logging.INFO, logger=hblink.LOGGER.name):
        for _ in range(5):
            hb._handle_unit_stream_start(sdr, sid(CONSOLE_RADIO), sid(RADIO), 1, b'\x0e' * 4)
    [line] = [m for m in caplog.messages if 'UNIT CALL rejected' in m]
    assert f'repeater {SDR1}' in line and 'heard at 461.68750 MHz CC1' in line and 'receive-only' in line
    assert f'our own call, being sent via {ROAM1}' in line and f'{SIMPLEX / 1e6:.5f} MHz' in line
