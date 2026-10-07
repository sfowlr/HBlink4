"""
Where a stream was heard: the channel and location it came from, per stream.

- a received stream records its peer's frequency / color code when it starts; a
  roaming peer sends DMRC right before a stream heard on a new channel, so the
  stream (and the radio's last-heard entry) keeps that channel after it retunes
- a channel is busy while a stream heard on it is open, wherever its peer is now
- DMRC's optional tail (HBlink4 extension) carries latitude / longitude / height
  above ground, as RPTC does; peers' locations go into last-heard and events
- stream_start events say where the stream was heard
"""
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hblink4 import hblink
from hblink4.access_control import RepeaterMatcher
from hblink4.utils import parse_location

from test_roaming import CHANNELS, OTHER, ROAM1, SIMPLEX, dmrt, roamer
from test_unit_call_routing import MODEM1, RADIO, make_hb, peer, rid, route, sid

BOT = 9990199
ADDR = ('10.0.0.9', 40000)


def dmrc(freq, cc=1, tail=b''):
    body = ('%-8.8s%09u%09u%02u%02u%c%-40.40s%-40.40s' % ('ROAM', freq, freq, 1, cc, '4', 'v', 's')).encode()
    return b'DMRC' + rid(ROAM1) + body + tail


def location_tail(lat='39.1031', lon='-84.51200', height='012'):
    return lat.encode().ljust(8)[:8] + lon.encode().ljust(9)[:9] + height.encode().ljust(3)[:3]


def registered_roamer(hb, freq=OTHER, tail=b''):
    hb.transport = type('T', (), {'sendto': lambda self, data, addr: None})()
    hb._matcher = RepeaterMatcher({'repeater_configurations': {'patterns': [
        {'name': 'roamers', 'description': '', 'match': {'ids': [ROAM1]},
         'config': {'passphrase': 'x', 'default_unit_calls': True, 'roaming': True}}]}})
    hb._handle_dmrc(rid(ROAM1), dmrc(freq, tail=tail), ADDR)
    r = hb._repeaters[rid(ROAM1)]
    r.sent = []
    r.send = r.sent.append
    return r


def hear_call(hb, r, stream_id=b'\x07' * 4, slot=2):
    assert hb._handle_unit_stream_start(r, sid(RADIO), sid(BOT), slot, stream_id)
    return r.get_slot_stream(slot)


def test_a_stream_keeps_the_channel_the_roamer_heard_it_on():
    hb = make_hb()
    r = registered_roamer(hb)
    hb._handle_dmrc(rid(ROAM1), dmrc(SIMPLEX), ADDR)      # stopped on SIMPLEX: DMRC before the stream
    stream = hear_call(hb, r)
    assert (stream.freq, stream.colorcode) == (SIMPLEX, 1)
    hb._handle_dmrc(rid(ROAM1), dmrc(OTHER), ADDR)        # moves on while the stream is still open
    assert (stream.freq, hb._user_cache.lookup(RADIO).freq) == (SIMPLEX, SIMPLEX)

    # Once the call's over, the radio is answered on SIMPLEX, not where the roamer is now.
    r.set_slot_stream(2, None)
    with patch.dict(hblink.CONFIG, CHANNELS):
        assert route(hb, slot=1)[0] == {rid(ROAM1)}
    assert dmrt(r)[-1][12:16] == SIMPLEX.to_bytes(4, 'big')


def test_a_channel_is_busy_while_a_stream_heard_on_it_is_open():
    hb = make_hb()
    r = registered_roamer(hb, freq=SIMPLEX)
    stream = hear_call(hb, r)
    hb._handle_dmrc(rid(ROAM1), dmrc(OTHER), ADDR)
    assert hb._channel_busy(SIMPLEX) and not hb._channel_busy(OTHER)
    stream.ended = True
    assert not hb._channel_busy(SIMPLEX)


def test_a_stream_we_send_counts_on_the_peers_channel():
    hb = make_hb()
    r = roamer(hb, ROAM1, freq=SIMPLEX)
    hear_call(hb, r).freq = None                          # e.g. an assumed (TX) stream
    assert hb._channel_busy(SIMPLEX) and not hb._channel_busy(OTHER)


def test_dmrc_location_tail():
    hb = make_hb()
    r = registered_roamer(hb, tail=location_tail())
    assert parse_location(r.latitude, r.longitude, r.height) == (39.1031, -84.512, 12)
    data = hb._prepare_repeater_event_data(rid(ROAM1), r)
    assert (data['latitude'], data['longitude'], data['height']) == (39.1031, -84.512, 12)

    hb._events.emitted.clear()
    hb._handle_dmrc(rid(ROAM1), dmrc(OTHER), ADDR)        # plain 119 bytes: location kept, nothing changed
    assert parse_location(r.latitude, r.longitude, r.height)[0] == 39.1031
    assert not [e for e in hb._events.emitted if e[0] == 'repeater_channel']

    hb._handle_dmrc(rid(ROAM1), dmrc(SIMPLEX, tail=location_tail(lat='39.2000')), ADDR)
    [(_, ev)] = [e for e in hb._events.emitted if e[0] == 'repeater_channel']
    assert ev == {'repeater_id': ROAM1, 'rx_freq': str(SIMPLEX), 'tx_freq': str(SIMPLEX), 'colorcode': '01',
                  'latitude': 39.2, 'longitude': -84.512, 'height': 12}


def test_a_stock_dmrc_peer_has_no_location():
    hb = make_hb()
    r = registered_roamer(hb)
    assert parse_location(r.latitude, r.longitude, r.height) == (None, None, None)
    assert hb._prepare_repeater_event_data(rid(ROAM1), r)['latitude'] is None


def test_last_heard_and_stream_start_say_where_it_was_heard():
    hb = make_hb()
    m = hb._repeaters[rid(MODEM1)]
    m.latitude, m.longitude, m.height = b'39.1031 ', b'-84.5120 ', b'030'     # as RPTC sends them
    assert hb._handle_unit_stream_start(m, sid(RADIO), sid(BOT), 1, b'\x05' * 4)
    entry = hb._user_cache.lookup(RADIO)
    assert (entry.freq, entry.colorcode, entry.latitude, entry.longitude, entry.height) == \
        (461_687_500, 1, 39.1031, -84.512, 30)
    assert entry.to_dict()['latitude'] == 39.1031
    [(_, ev)] = [e for e in hb._events.emitted if e[0] == 'stream_start']
    assert (ev['repeater_id'], ev['freq'], ev['colorcode'], ev['latitude'], ev['longitude'], ev['height']) == \
        (MODEM1, 461_687_500, 1, 39.1031, -84.512, 30)


def test_group_and_data_streams_are_stamped_too():
    hb = make_hb()
    m = hb._repeaters[rid(MODEM1)]
    stream = hblink.StreamState(repeater_id=m.repeater_id, rf_src=sid(RADIO), dst_id=sid(9), slot=1,
                                start_time=0, last_seen=0, stream_id=b'\x06' * 4)
    hb._stamp_channel(stream, hb._peer_channel(MODEM1))
    assert (stream.freq, stream.colorcode) == (461_687_500, 1)
    assert hb._peer_channel(0) == {'freq': None, 'colorcode': None, 'latitude': None, 'longitude': None,
                                   'height': None}


def test_parse_location():
    assert parse_location(b'0.0000  ', b'0.0000   ', b'000') == (None, None, None)   # unconfigured hotspot
    assert parse_location(b'', b'') == (None, None, None)
    assert parse_location(b'91.0', b'10.0') == (None, None, None)
    assert parse_location(b'-33.8688', b'151.2093', b'') == (-33.8688, 151.2093, None)
    assert parse_location('39.1\x00\x00\x00\x00', '-84.5', '7') == (39.1, -84.5, 7)
