"""
Receiver voting (global.voting, voting.py): one voice call heard by several peers goes out once,
burst by burst from the best copy.

- with one receiver, every burst goes straight out, unchanged
- a burst waits for the other receivers' copies, at most `hold_ms` after the first came in
- bursts a receiver lost come from another one; bursts every receiver lost are skipped
- each AMBE frame comes from the copy with the fewest FEC errors in it
- bursts line up by letter and time, and by content when a receiver is half a superframe behind
- a terminator from any receiver ends the call, after the bursts before it
- in HBlink4: the second peer's copy joins the first one's vote (no contention, no routing of its
  own); targets get one stream; a peer that hears the call itself isn't sent it
"""
import os
import random
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hblink4 import hblink
from hblink4 import voting
from hblink4.voting import Vote, frame_errors, _C0_BITS, _C1_BITS, _golay_parity, _prng, _EMB_CODES

from test_unit_call_routing import make_hb, rid, sid
from test_sites import CONFIG, CH1, SDR_NY, ROAM_AL, TALKER, dmrt, sdr, two_sites

SDR_NY2 = 3126012
SYNC_A = 0x755FD7DF75F7
STREAM_A, STREAM_B = 0xA1A1A1A1, 0xB2B2B2B2


# ---- building bursts ----

def ambe(seed: int) -> int:
    """A clean 72-bit AMBE+2 channel frame."""
    rnd = random.Random(seed)
    d0, d1 = rnd.getrandbits(12), rnd.getrandbits(12)
    w0 = ((d0 << 11) | _golay_parity(d0)) << 1
    w0 |= bin(w0).count('1') & 1
    w1 = ((d1 << 11) | _golay_parity(d1)) ^ _prng(d0)
    f = rnd.getrandbits(72)                        # C2/C3 bits stay random
    for shift, bit in _C0_BITS:
        f = (f & ~(1 << shift)) | (((w0 >> bit) & 1) << shift)
    for shift, bit in _C1_BITS:
        f = (f & ~(1 << shift)) | (((w1 >> bit) & 1) << shift)
    assert frame_errors(f) == 0
    return f


def garble(f: int, n: int, which=_C0_BITS) -> int:
    for shift, _ in which[:n]:
        f ^= 1 << shift
    return f


def payload(pos: int, errs=(0, 0, 0)) -> bytes:
    frames = [garble(ambe(pos * 3 + i), errs[i]) for i in range(3)]
    voice = (frames[0] << 144) | (frames[1] << 72) | frames[2]
    letter = pos % 6
    center = SYNC_A if letter == 0 else ((_EMB_CODES[0x23] >> 8) << 40) | (_EMB_CODES[0x23] & 0xFF)
    return (((voice >> 108) << 156) | (center << 108) | (voice & ((1 << 108) - 1))).to_bytes(33, 'big')


def dmrd(peer: int, pos: int, stream: int, slot=1, errs=(0, 0, 0), src=TALKER, dst=201, seq=0) -> bytes:
    letter = pos % 6
    flags = (0x80 if slot == 2 else 0) | ((0x10 | 0) if letter == 0 else letter)
    return (b'DMRD' + bytes([seq & 0xFF]) + sid(src) + sid(dst) + rid(peer) + bytes([flags])
            + stream.to_bytes(4, 'big') + payload(pos, errs) + b'\0\0')


def header(peer: int, stream: int, slot=1, terminator=False) -> bytes:
    flags = (0x80 if slot == 2 else 0) | 0x20 | (2 if terminator else 1)
    return (b'DMRD\0' + sid(TALKER) + sid(201) + rid(peer) + bytes([flags]) + stream.to_bytes(4, 'big')
            + b'\0' * 33 + b'\0\0')


def feed(vote, peer, packet, now):
    ft, dv = (packet[15] & 0x30) >> 4, packet[15] & 0x0F
    return vote.add(rid(peer), packet, ft, dv, ft == 2 and dv == 2, now)


def positions(out):
    """Which burst (by its first AMBE frame) each voice packet out is."""
    got = []
    for packet, end, _ in out:
        if end or (packet[15] & 0x30) == 0x20:
            continue
        p = int.from_bytes(packet[20:53], 'big')
        f0 = p >> (264 - 72)
        got.append(next(n for n in range(200) if f0 == ambe(n * 3)))
    return got


# ---- the vote ----

def test_one_receiver_goes_straight_through():
    v = Vote(('k',), 0.15, 0.0)
    v.join(rid(1), b'a', 0.0)
    out = []
    for pos in range(12):
        sent = feed(v, 1, dmrd(1, pos, STREAM_A), pos * 0.06)
        assert len(sent) == 1 and sent[0][0] == dmrd(1, pos, STREAM_A)
        out += sent
    assert positions(out) == list(range(12))


def test_gaps_in_each_copy_are_filled_from_the_other():
    v = Vote(('k',), 0.15, 0.0)
    v.join(rid(1), b'a', 0.0)
    v.join(rid(2), b'b', 0.0)
    a_lost, b_lost = {3, 4, 5, 13}, {0, 1, 9, 10, 17}
    events = [(pos * 0.06, 1, pos) for pos in range(18) if pos not in a_lost]
    events += [(pos * 0.06 + 0.08, 2, pos) for pos in range(18) if pos not in b_lost]   # 80 ms behind
    out = []
    for t, peer, pos in sorted(events):
        out += feed(v, peer, dmrd(peer, pos, STREAM_A if peer == 1 else STREAM_B), t)
        out += v.poll(t)
    out += v.poll(10.0)
    assert positions(out) == list(range(18))
    assert v.lost == 0 and v.late == 1               # B's first burst: A's was out before B was heard


def test_each_ambe_frame_comes_from_the_cleaner_copy():
    v = Vote(('k',), 0.15, 0.0)
    v.join(rid(1), b'a', 0.0)
    v.join(rid(2), b'b', 0.0)
    feed(v, 1, dmrd(1, 0, STREAM_A), 0.0)
    feed(v, 2, dmrd(2, 0, STREAM_B), 0.01)
    feed(v, 1, dmrd(1, 1, STREAM_A, errs=(3, 0, 2)), 0.06)
    [(packet, _, _)] = feed(v, 2, dmrd(2, 1, STREAM_B, errs=(0, 2, 0)), 0.07)
    p = int.from_bytes(packet[20:53], 'big')
    voice = ((p >> 156) << 108) | (p & ((1 << 108) - 1))
    frames = (voice >> 144, (voice >> 72) & ((1 << 72) - 1), voice & ((1 << 72) - 1))
    assert [frame_errors(f) for f in frames] == [0, 0, 0]


def test_a_burst_waits_for_the_other_copy_at_most_the_hold():
    v = Vote(('k',), 0.15, 0.0)
    v.join(rid(1), b'a', 0.0)
    v.join(rid(2), b'b', 0.0)
    assert feed(v, 1, dmrd(1, 0, STREAM_A), 0.0)              # B has nothing yet: not awaited
    feed(v, 2, dmrd(2, 0, STREAM_B), 0.02)                     # late: already out
    assert v.late == 1
    assert feed(v, 1, dmrd(1, 1, STREAM_A), 0.06) == []        # waits for B
    assert v.poll(0.20) == []
    assert v.next_deadline() == 0.06 + 0.15
    assert positions(v.poll(0.21)) == [1]                      # B never sent it
    assert positions(feed(v, 1, dmrd(1, 2, STREAM_A), 0.62)) == [2]      # B quiet 0.6 s: lost the call


def test_a_receiver_half_a_superframe_behind_lines_up_by_content():
    v = Vote(('k',), 0.5, 0.0)
    v.join(rid(1), b'a', 0.0)
    v.join(rid(2), b'b', 0.0)
    out = []
    events = [(pos * 0.06, 1, pos) for pos in range(14)]
    events += [(pos * 0.06 + 0.25, 2, pos) for pos in range(2, 14)]     # joins late, 250 ms behind
    for t, peer, pos in sorted(events):
        out += feed(v, peer, dmrd(peer, pos, STREAM_A if peer == 1 else STREAM_B), t)
    out += v.poll(10.0)
    assert positions(out) == list(range(14))
    assert v.members[rid(2)].last_pos == 13


def test_a_terminator_ends_the_call_after_the_bursts_before_it():
    v = Vote(('k',), 0.15, 0.0)
    v.join(rid(1), b'a', 0.0)
    v.join(rid(2), b'b', 0.0)
    out = feed(v, 1, header(1, STREAM_A), 0.0)
    assert out and not out[0][1]
    assert feed(v, 2, header(2, STREAM_B), 0.01) == []         # one peer's headers only
    for pos in range(6):
        out += feed(v, 1, dmrd(1, pos, STREAM_A), 0.06 * pos + 0.06)
        if pos < 4:
            out += feed(v, 2, dmrd(2, pos, STREAM_B), 0.06 * pos + 0.1)
    out += feed(v, 1, header(1, STREAM_A, terminator=True), 0.42)
    assert not v.done                                           # B's copy of 4 and 5 could still come
    out += feed(v, 2, dmrd(2, 4, STREAM_B), 0.43)
    out += feed(v, 2, dmrd(2, 5, STREAM_B), 0.44)
    assert v.done and out[-1][1]
    assert positions(out) == list(range(6))
    assert feed(v, 2, header(2, STREAM_B, terminator=True), 0.5) == []


# ---- in HBlink4 ----

VOTING = {'global': dict(CONFIG['global'], voting={'enabled': True, 'hold_ms': 150})}


def two_receivers(hb):
    roam_al = two_sites(hb)                               # SDR_NY hears Ch 1 at ny; a roamer at al
    sdr(hb, SDR_NY2, CH1, site='ny')
    return roam_al


def send(hb, peer, packet, now):
    r = hb._repeaters[rid(peer)]
    with patch.object(hblink, 'time', lambda: now):
        hb._handle_dmr_data(packet, (r.ip, r.port))


def poll(hb, now):
    with patch.object(hblink, 'time', lambda: now):
        for vote in list(hb._votes.values()):
            hb._vote_poll(vote)


def test_two_receivers_one_call_out():
    hb = make_hb()
    roam = two_receivers(hb)
    with patch.dict(hblink.CONFIG, VOTING):
        send(hb, SDR_NY, header(SDR_NY, STREAM_A), 0.0)
        send(hb, SDR_NY2, header(SDR_NY2, STREAM_B), 0.01)
        assert hb._repeaters[rid(SDR_NY2)].get_slot_stream(1).vote is hb._repeaters[rid(SDR_NY)].get_slot_stream(1).vote
        for pos in range(12):
            t = 0.06 * (pos + 1)
            if pos not in (2, 3):
                send(hb, SDR_NY, dmrd(SDR_NY, pos, STREAM_A), t)
            if pos not in (7, 11):
                send(hb, SDR_NY2, dmrd(SDR_NY2, pos, STREAM_B, slot=1), t + 0.03)
            poll(hb, t + 0.03)
        send(hb, SDR_NY2, header(SDR_NY2, STREAM_B, terminator=True), 0.8)
        poll(hb, 0.81)
        send(hb, SDR_NY, header(SDR_NY, STREAM_A, terminator=True), 0.82)
    out = [p for p in roam.sent if p[:4] == b'DMRD']
    assert {p[16:20] for p in out} == {STREAM_A.to_bytes(4, 'big')}          # one stream, the first's
    assert positions([(p, False, b'') for p in out]) == list(range(12))
    assert (out[-1][15] & 0x0F) == 2 and len([p for p in out if (p[15] & 0x2F) == 0x22]) == 1
    assert not hb._votes
    assert [p[4] for p in out] == list(range(len(out)))                       # our own sequence


def test_without_voting_the_second_copy_goes_nowhere():
    hb = make_hb()
    roam = two_receivers(hb)
    with patch.dict(hblink.CONFIG, CONFIG):
        send(hb, SDR_NY, header(SDR_NY, STREAM_A), 0.0)
        send(hb, SDR_NY2, header(SDR_NY2, STREAM_B), 0.01)
        for pos in range(3):
            send(hb, SDR_NY2, dmrd(SDR_NY2, pos, STREAM_B), 0.06 * (pos + 1))
    assert hb._repeaters[rid(SDR_NY2)].get_slot_stream(1).vote is None
    assert {p[16:20] for p in roam.sent if p[:4] == b'DMRD'} == {STREAM_A.to_bytes(4, 'big')}


def test_a_peer_that_hears_the_call_itself_isnt_sent_it():
    hb = make_hb()
    roam = two_receivers(hb)
    with patch.dict(hblink.CONFIG, VOTING):
        send(hb, SDR_NY, header(SDR_NY, STREAM_A), 0.0)
        first = hb._repeaters[rid(SDR_NY)].get_slot_stream(1)
        assert rid(ROAM_AL) in first.target_repeaters
        roam.site = 'ny'                                  # say the roamer heard it on Ch 1 at ny
        roam.roaming_idle = (CH1, 2)
        send(hb, ROAM_AL, header(ROAM_AL, 0xC3C3C3C3, slot=2), 0.02)
    assert rid(ROAM_AL) not in first.target_repeaters
