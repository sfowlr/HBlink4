"""
Sites: one channel plan in use at several places too far apart to hear each other.

Unit calls
- go out only at the site the target radio was heard at: fixed peers and roamers
  elsewhere are never used, and traffic elsewhere doesn't make the channel busy
- a site is a pattern's `site`; without names on both sides, the locations
  (within global.site_radius_km); without either, it's the same site
- an external last-heard report places the radio by its `site` or location

Group calls
- a peer transmitting on the talker's channel at the talker's site is left out
- roamers carry the call to the other sites, one per site, as their
  `roaming_group_calls` allows: "all", "idle" (their idle channel, from DMRC) or "none"
- never to a site with a fixed peer on the channel, or traffic on it
- a call from the network (no channel) goes out on the roaming channel with its `tg`
- our own transmission heard back at the site we sent it at isn't forwarded,
  and doesn't move the radio in the user cache
"""
import json
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hblink4 import hblink
from hblink4.access_control import RepeaterConfig
from hblink4.external_last_heard import ExternalLastHeard
from hblink4.models import StreamState
from hblink4.utils import distance_km, same_site

from test_unit_call_routing import CONSOLE_PEER, RADIO, make_hb, peer, rid, route, sid

CH1, CH15 = 467_375_000, 461_687_500
AL, NY = (33.0, -85.0), (43.027, -77.715)
SDR_AL, SDR_NY, ROAM_AL, ROAM_NY, ROAM_NY2, MODEM_NY = 3126001, 3126002, 3127001, 3127002, 3127003, 3126009
CONFIG = {'global': {'roaming_channels': [
    {'freq': CH1, 'cc': 2, 'tg': 201},
    {'freq': CH15, 'cc': 2, 'tg': 215},
]}}
TALKER = 3135412


def locate(r, at):
    r.latitude, r.longitude = (f'{at[0]:.4f}'.encode(), f'{at[1]:.4f}'.encode())
    return r


def sdr(hb, n, freq, at=None, site=None):
    r = peer(hb, n, freq=freq, cc=2, tx=False, unit=False)
    r.tx_freq = r.rx_freq
    r.site = site
    return locate(r, at) if at else r


def roamer(hb, n, freq=CH1, at=None, site=None, group=None, idle=None):
    r = peer(hb, n, cc=2)
    r.rx_freq = r.tx_freq = str(freq).encode()
    r.roaming, r.site, r.roaming_group_calls = True, site, group
    r.roaming_idle = (idle, 2) if idle else None
    return locate(r, at) if at else r


def dmrt(r):
    return [p for p in r.sent if p[:4] == b'DMRT']


def heard_by(hb, radio, n):
    hb._user_cache.update(radio_id=radio, repeater_id=n, callsign='', slot=1, talkgroup=9,
                          **hb._peer_channel(n))


# ---- sites ----

def test_same_site_by_name_then_distance_then_unknown():
    assert same_site('al', None, None, 'al', *NY, 20)              # names win over locations
    assert not same_site('al', *AL, 'ny', *AL, 20)
    assert not same_site('al', *AL, None, *NY, 20)                 # one named: locations decide
    assert same_site(None, *AL, None, AL[0] + 0.1, AL[1], 20)      # 11 km
    assert not same_site(None, *AL, None, AL[0] + 0.3, AL[1], 20)  # 33 km
    assert same_site(None, None, None, 'ny', *NY, 20)              # unknown: same site
    assert 1250 < distance_km(*AL, *NY) < 1400


def test_site_comes_from_the_pattern():
    assert RepeaterConfig(passphrase='x', site='al').site == 'al'
    assert RepeaterConfig(passphrase='x').roaming_group_calls is None
    try:
        RepeaterConfig(passphrase='x', roaming_group_calls='some')
    except ValueError:
        pass
    else:
        raise AssertionError('bad roaming_group_calls accepted')


# ---- unit calls ----

def test_a_reply_goes_out_the_roamer_at_the_radios_site_even_if_another_is_on_the_channel():
    hb = make_hb()
    sdr(hb, SDR_AL, CH15, at=AL)
    roam_al = roamer(hb, ROAM_AL, freq=CH1, at=AL)
    roam_ny = roamer(hb, ROAM_NY, freq=CH15, at=NY)           # idling on the radio's channel, wrong site
    heard_by(hb, RADIO, SDR_AL)
    with patch.dict(hblink.CONFIG, CONFIG):
        assert route(hb)[0] == {rid(ROAM_AL)}
    assert dmrt(roam_al) and not dmrt(roam_ny)


def test_no_roamer_at_the_site_fails_rather_than_going_out_elsewhere():
    hb = make_hb()
    sdr(hb, SDR_AL, CH15, site='al')
    roam_ny = roamer(hb, ROAM_NY, freq=CH15, site='ny')
    heard_by(hb, RADIO, SDR_AL)
    with patch.dict(hblink.CONFIG, CONFIG):
        assert route(hb) == (set(), False, {})
    assert not dmrt(roam_ny)
    assert hb._route_reason[b'\x01\x02\x03\x04'] == 'no roaming transceiver free'


def test_a_fixed_peer_at_another_site_neither_takes_the_call_nor_blocks_the_roamer():
    hb = make_hb()
    sdr(hb, SDR_AL, CH15, site='al')
    modem_ny = peer(hb, MODEM_NY, freq=CH15, cc=2)
    modem_ny.tx_freq, modem_ny.site = modem_ny.rx_freq, 'ny'
    roamer(hb, ROAM_AL, site='al')
    heard_by(hb, RADIO, SDR_AL)
    with patch.dict(hblink.CONFIG, CONFIG):
        assert route(hb)[0] == {rid(ROAM_AL)}


def test_traffic_on_the_channel_at_another_site_doesnt_make_it_busy():
    hb = make_hb()
    sdr(hb, SDR_AL, CH15, site='al')
    sdr_ny = sdr(hb, SDR_NY, CH15, site='ny')
    roamer(hb, ROAM_AL, site='al')
    sdr_ny.set_slot_stream(1, StreamState(repeater_id=rid(SDR_NY), rf_src=sid(1), dst_id=sid(2), slot=1,
                                          start_time=0, last_seen=0, stream_id=b'\x09' * 4))
    heard_by(hb, RADIO, SDR_AL)
    with patch.dict(hblink.CONFIG, CONFIG):
        assert route(hb)[0] == {rid(ROAM_AL)}
        sdr_ny.site = 'al'                                        # now it's next door
        assert route(hb, stream=b'\x05' * 4) == (set(), False, {})


def test_an_external_report_places_the_radio_by_location():
    hb = make_hb()
    roamer(hb, ROAM_AL, at=AL)
    roamer(hb, ROAM_NY, at=NY)
    feed = ExternalLastHeard(hb._user_cache)
    feed.handle('hblink4/last_heard/1', json.dumps(
        {'radio_id': RADIO, 'freq': CH15, 'colorcode': 2, 'latitude': NY[0], 'longitude': NY[1]}).encode())
    entry = hb._user_cache.lookup(RADIO)
    assert (entry.latitude, entry.site) == (NY[0], None)
    with patch.dict(hblink.CONFIG, CONFIG):
        assert route(hb)[0] == {rid(ROAM_NY)}
    feed.handle('hblink4/last_heard/1', json.dumps({'radio_id': RADIO, 'freq': CH15, 'colorcode': 2, 'site': 'al'}).encode())
    hb._repeaters[rid(ROAM_AL)].site = 'al'
    hb._repeaters[rid(ROAM_NY)].site = 'ny'
    with patch.dict(hblink.CONFIG, CONFIG):
        assert route(hb, stream=b'\x05' * 4)[0] == {rid(ROAM_AL)}


def test_a_report_without_a_place_keeps_the_one_known_for_that_channel():
    hb = make_hb()
    sdr(hb, SDR_NY, CH15, site='ny')
    roamer(hb, ROAM_AL, site='al')
    roamer(hb, ROAM_NY, site='ny')
    heard_by(hb, RADIO, SDR_NY)
    feed = ExternalLastHeard(hb._user_cache)
    feed.handle('t', json.dumps({'radio_id': RADIO, 'freq': CH15, 'colorcode': 2}).encode())
    assert (hb._user_cache.lookup(RADIO).site, hb._user_cache.lookup(RADIO).source) == ('ny', 'external-ext')
    with patch.dict(hblink.CONFIG, CONFIG):
        assert route(hb)[0] == {rid(ROAM_NY)}
    feed.handle('t', json.dumps({'radio_id': RADIO, 'freq': CH1, 'colorcode': 2}).encode())
    assert hb._user_cache.lookup(RADIO).site is None                 # another channel: place unknown


# ---- group calls ----

def group_call(hb, n, dst=201, src=TALKER, stream=b'\xaa\xbb\xcc\xdd'):
    r = hb._repeaters[rid(n)]
    assert hb._handle_stream_start(r, sid(src), sid(dst), 1, stream, call_type_bit=0)
    return r.get_slot_stream(1)


def two_sites(hb, group='all', idle=None):
    sdr(hb, SDR_AL, CH1, site='al')
    sdr(hb, SDR_NY, CH1, site='ny')
    return roamer(hb, ROAM_AL, freq=CH15, site='al', group=group, idle=idle)


def test_a_group_call_heard_at_one_site_goes_out_a_roamer_at_the_other():
    hb = make_hb()
    roam_al = two_sites(hb)
    roam_ny = roamer(hb, ROAM_NY, site='ny', group='all')
    with patch.dict(hblink.CONFIG, CONFIG):
        stream = group_call(hb, SDR_NY)
    assert rid(ROAM_AL) in stream.target_repeaters and rid(ROAM_NY) not in stream.target_repeaters
    assert stream.target_slots == {rid(ROAM_AL): 2}
    [pkt] = dmrt(roam_al)
    assert pkt[12:16] == CH1.to_bytes(4, 'big') and pkt[16] == 2
    assert not dmrt(roam_ny)


def test_one_roamer_per_site():
    hb = make_hb()
    two_sites(hb)
    roamer(hb, 3127009, site='al', group='all')
    with patch.dict(hblink.CONFIG, CONFIG):
        stream = group_call(hb, SDR_NY)
    assert len([t for t in stream.target_repeaters if hb._repeaters[t].roaming]) == 1


def test_roaming_group_calls_all_idle_none():
    for group, idle, global_default, sent in [
            ('all', None, None, True),
            ('idle', CH1, None, True),
            ('idle', CH15, None, False),          # its idle channel isn't the call's
            ('idle', None, None, False),          # no idle channel reported
            ('none', None, 'all', False),
            (None, None, None, False),            # the default is none
            (None, None, 'all', True)]:
        hb = make_hb()
        roam_al = two_sites(hb, group=group, idle=idle)
        cfg = {'global': dict(CONFIG['global'], **({'roaming_group_calls': global_default} if global_default else {}))}
        with patch.dict(hblink.CONFIG, cfg):
            group_call(hb, SDR_NY)
        assert bool(dmrt(roam_al)) == sent, (group, idle, global_default)


def test_not_to_a_site_with_a_fixed_peer_on_the_channel_or_traffic_on_it():
    hb = make_hb()
    roam_al = two_sites(hb)
    modem = peer(hb, MODEM_NY, freq=CH1, cc=2)
    modem.tx_freq, modem.site = modem.rx_freq, 'al'
    with patch.dict(hblink.CONFIG, CONFIG):
        stream = group_call(hb, SDR_NY)
    assert rid(MODEM_NY) in stream.target_repeaters and not dmrt(roam_al)

    hb = make_hb()
    roam_al = two_sites(hb)
    hb._repeaters[rid(SDR_AL)].set_slot_stream(2, StreamState(
        repeater_id=rid(SDR_AL), rf_src=sid(1), dst_id=sid(2), slot=2, start_time=0, last_seen=0,
        stream_id=b'\x09' * 4))
    with patch.dict(hblink.CONFIG, CONFIG):
        group_call(hb, SDR_NY)
    assert not dmrt(roam_al)


def test_a_fixed_peer_on_the_talkers_channel_at_the_talkers_site_is_left_out():
    hb = make_hb()
    sdr(hb, SDR_NY, CH1, site='ny')
    same = peer(hb, MODEM_NY, freq=CH1, cc=2)
    same.tx_freq, same.site = same.rx_freq, 'ny'
    other_channel = peer(hb, 3126010, freq=CH15, cc=2)
    other_channel.tx_freq, other_channel.site = other_channel.rx_freq, 'ny'
    with patch.dict(hblink.CONFIG, CONFIG):
        stream = group_call(hb, SDR_NY)
    assert rid(MODEM_NY) not in stream.target_repeaters
    assert rid(3126010) in stream.target_repeaters


def test_a_call_from_the_network_goes_out_on_its_talkgroups_channel_at_every_site():
    hb = make_hb()
    roam_al = roamer(hb, ROAM_AL, site='al', group='all')
    roam_ny = roamer(hb, ROAM_NY, site='ny', group='all')
    with patch.dict(hblink.CONFIG, CONFIG):
        stream = group_call(hb, CONSOLE_PEER, dst=215)
        assert {rid(ROAM_AL), rid(ROAM_NY)} <= stream.target_repeaters
        assert dmrt(roam_al)[0][12:16] == CH15.to_bytes(4, 'big')
    hb = make_hb()
    roam_al = roamer(hb, ROAM_AL, site='al', group='all')
    with patch.dict(hblink.CONFIG, CONFIG):
        group_call(hb, CONSOLE_PEER, dst=299)                         # no roaming channel for it
    assert not dmrt(roam_al)


def test_a_refused_group_call_moves_to_another_roamer_at_that_site_only():
    hb = make_hb()
    roam_al = two_sites(hb)
    roam_al2 = roamer(hb, 3127009, site='al', group='all')
    roam_al2.roaming_priority = 200
    roamer(hb, ROAM_NY2, site='ny', group='all', freq=CH15)
    with patch.dict(hblink.CONFIG, CONFIG):
        stream = group_call(hb, SDR_NY)
        hb._handle_roaming_ack(rid(ROAM_AL), b'DMRK' + rid(ROAM_AL) + b'\xaa\xbb\xcc\xdd' + bytes([4])
                               + CH1.to_bytes(4, 'big') + bytes([2]))
    assert rid(3127009) in stream.target_repeaters and rid(ROAM_AL) not in stream.target_repeaters
    assert not [e for e in hb._events.emitted if e[0] in ('unit_call_unroutable', 'unit_call_status')]


def test_our_own_transmission_heard_back_goes_nowhere_and_doesnt_move_the_radio():
    hb = make_hb()
    roam_al = two_sites(hb)
    with patch.dict(hblink.CONFIG, CONFIG):
        stream = group_call(hb, SDR_NY)
        roam_al.set_slot_stream(2, StreamState(
            repeater_id=rid(ROAM_AL), rf_src=sid(TALKER), dst_id=sid(201), slot=2, start_time=0,
            last_seen=0, stream_id=stream.stream_id, is_assumed=True))
        echo = group_call(hb, SDR_AL, stream=b'\x0e' * 4)        # AL's SDR hears ROAM_AL
    assert echo.target_repeaters == set()
    assert hb._user_cache.lookup(TALKER).repeater_id == SDR_NY


def test_roamers_report_their_idle_channel_on_dmrc():
    hb = make_hb()
    r = roamer(hb, ROAM_AL)
    tail = b'33.0000 -85.0000 002' + b'%09u' % CH1 + b'02'
    hb._parse_dmrc_location(r, b'\x00' * 119 + tail)
    assert r.roaming_idle == (CH1, 2) and r.latitude == b'33.0000 '
