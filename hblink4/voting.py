"""
Receiver voting: one transmission heard by several peers (receivers at one
site where terrain splits the coverage, or at several sites) goes out once,
built frame by frame from the best copy of each.

A `Vote` is fed every peer's copy of a voice call and gives back the packets
to forward, in order:

- Voice bursts are lined up by position: the burst letter (A–F) fixes a
  position to within a superframe (360 ms), and the arrival time (less that
  peer's latency) or a matching copy already in picks the superframe.
- Each position goes out once every peer still sending has delivered it (at
  once, with one peer), or `hold_s` after the first copy came in. A position
  no peer delivered is skipped. A peer quiet for QUIET_S (or two holds) has
  lost the call and isn't waited for.
- Of the copies of a position, each of its three AMBE frames comes from the
  copy with the fewest FEC errors in that frame (Golay C0 and C1; an
  uncorrectable C0 counts as many), and the sync / EMB field in the middle
  from the copy where it is cleanest. Ties: the copy best overall, then the
  BER the peer reported (DMRD byte 53), then the peer that joined first.
- Headers before the voice go out from the first peer that sent one; a
  terminator from any peer ends the vote, after what came before it.

Pure: no I/O, time passed in. HBProtocol does the forwarding (hblink.py,
`_vote_*`).
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

BURST_S = 0.06
SUPERFRAME = 6
CONTENT_MATCH_BITS = 50      # of 216 voice bits: the same burst (random ones differ by ~108)
UNCORRECTABLE = 8            # what an AMBE frame with an uncorrectable C0 scores
QUIET_S = 0.5               # a peer quiet at least this long (or two holds) isn't waited for
HISTORY = 36                 # positions sent, kept for content matching (2.2 s)


# ---- FEC: Golay(23,12) and AMBE+2 frames ----

_GOLAY_POLY = 0xC75


def _golay_parity(data12: int) -> int:
    reg = data12 << 11
    for i in range(22, 10, -1):
        if reg & (1 << i):
            reg ^= _GOLAY_POLY << (i - 11)
    return reg & 0x7FF


def _syndromes() -> List[int]:
    table = [0] * 2048
    for a in range(-1, 23):
        for b in range(a + 1 if a >= 0 else -1, 23):
            for c in range(b + 1 if b >= 0 else -1, 23):
                e = sum(1 << i for i in (a, b, c) if i >= 0)
                table[_golay_parity(e >> 11) ^ (e & 0x7FF)] = e
    return table


_SYNDROME = _syndromes()


def golay23_errors(word23: int) -> Tuple[int, int]:
    """(corrected 12 data bits, bits corrected) for a Golay(23,12) word, data in the top 12 bits."""
    err = _SYNDROME[_golay_parity(word23 >> 11) ^ (word23 & 0x7FF)]
    return (word23 ^ err) >> 11, bin(err).count('1')


# DMR AMBE+2 de-interleave (DSD dmr_const.h rW/rX/rY/rZ): channel-frame dibit i → (row, bit)
_RW = (0, 1, 0, 1, 0, 1, 0, 1, 0, 1, 0, 1, 0, 1, 0, 1, 0, 1,
       0, 1, 0, 1, 0, 2, 0, 2, 0, 2, 0, 2, 0, 2, 0, 2, 0, 2)
_RX = (23, 10, 22, 9, 21, 8, 20, 7, 19, 6, 18, 5, 17, 4, 16, 3, 15, 2,
       14, 1, 13, 0, 12, 10, 11, 9, 10, 8, 9, 7, 8, 6, 7, 5, 6, 4)
_RY = (0, 2, 0, 2, 0, 2, 0, 2, 0, 3, 0, 3, 1, 3, 1, 3, 1, 3,
       1, 3, 1, 3, 1, 3, 1, 3, 1, 3, 1, 3, 1, 3, 1, 3, 1, 3)
_RZ = (5, 3, 4, 2, 3, 1, 2, 0, 1, 13, 0, 12, 22, 11, 21, 10, 20, 9,
       19, 8, 18, 7, 17, 6, 16, 5, 15, 4, 14, 3, 13, 2, 12, 1, 11, 0)

# frame bit k (MSB first) → its place in C0 (24 bits) or C1 (23 bits), or None (C2/C3: unprotected)
_C0_BITS: List[Tuple[int, int]] = []     # (shift in the 72-bit frame int, bit in C0)
_C1_BITS: List[Tuple[int, int]] = []
for _i in range(36):
    for _k, (_row, _bit) in ((2 * _i, (_RW[_i], _RX[_i])), (2 * _i + 1, (_RY[_i], _RZ[_i]))):
        if _row == 0:
            _C0_BITS.append((71 - _k, _bit))
        elif _row == 1:
            _C1_BITS.append((71 - _k, _bit))


def _prng(c0_data: int) -> int:
    """C1's scrambling (23 bits), seeded from C0's data."""
    pr, mask = 16 * c0_data, 0
    for _ in range(23):
        pr = (173 * pr + 13849) & 0xFFFF
        mask = (mask << 1) | (pr >> 15)
    return mask


def frame_errors(frame72: int) -> int:
    """FEC errors in one 72-bit AMBE+2 channel frame: C0's (Golay(24,12): beyond three it
    scores UNCORRECTABLE) plus C1's (Golay(23,12), after descrambling)."""
    c0 = c1 = 0
    for shift, bit in _C0_BITS:
        c0 |= ((frame72 >> shift) & 1) << bit
    for shift, bit in _C1_BITS:
        c1 |= ((frame72 >> shift) & 1) << bit
    data0, e0 = golay23_errors(c0 >> 1)                       # bit 0 is the overall parity
    if (bin(c0).count('1') + e0) & 1:                         # parity disagrees after correcting
        if e0 == 3:
            return UNCORRECTABLE
        e0 += 1
    _, e1 = golay23_errors(c1 ^ _prng(data0))
    return e0 + e1


# ---- bursts ----

_MASK72, _MASK108, _MASK48 = (1 << 72) - 1, (1 << 108) - 1, (1 << 48) - 1
_AUDIO_SYNCS = (0x755FD7DF75F7, 0x7F7D5DD57DFD, 0x5D577F7757FF, 0x7DFFD5F55D5F)   # BS, MS, direct TS1/TS2
_EMB_BASIS = (0x0273, 0x04E5, 0x09C9, 0x11E2, 0x21B7, 0x411E, 0x804F)            # QR(16,7) rows
_EMB_CODES = []
for _v in range(128):
    _w = 0
    for _b in range(7):
        if _v >> _b & 1:
            _w ^= _EMB_BASIS[_b]
    _EMB_CODES.append(_w)


def burst_letter(frame_type: int, dtype_vseq: int) -> Optional[int]:
    """0–5 for voice bursts A–F (DMRD frame type 1 = voice sync, 0 = voice with the letter), else None."""
    if frame_type == 1:
        return 0
    if frame_type == 0 and 1 <= dtype_vseq <= 5:
        return dtype_vseq
    return None


def center_errors(center48: int, letter: int) -> int:
    """Bit errors in the middle 48 bits: the audio sync in burst A, the EMB around the embedded
    signalling in B–F (whose own bits aren't checked here)."""
    if letter == 0:
        return min(bin(center48 ^ s).count('1') for s in _AUDIO_SYNCS)
    emb = ((center48 >> 40) << 8) | (center48 & 0xFF)
    return min(bin(emb ^ c).count('1') for c in _EMB_CODES)


@dataclass
class Copy:
    """One peer's copy of a voice burst."""
    peer: bytes
    packet: bytes            # the DMRD packet as the peer sent it
    order: int               # when its peer joined the vote (0 first)
    frames: Tuple[int, int, int]
    voice: int               # the 216 voice bits
    center: int
    errors: Tuple[int, int, int]
    center_err: int

    @property
    def total(self) -> int:
        return sum(self.errors) + self.center_err

    @property
    def ber(self) -> int:
        return self.packet[53] if len(self.packet) > 53 else 0


def make_copy(peer: bytes, packet: bytes, letter: int, order: int) -> Copy:
    p = int.from_bytes(packet[20:53], 'big')
    voice = ((p >> 156) << 108) | (p & _MASK108)
    frames = (voice >> 144, (voice >> 72) & _MASK72, voice & _MASK72)
    center = (p >> 108) & _MASK48
    return Copy(peer, packet, order, frames, voice, center,
                tuple(frame_errors(f) for f in frames), center_errors(center, letter))


def splice(copies: List[Copy]) -> Tuple[bytes, Copy]:
    """The burst built from the best of each part of `copies`, and the copy best overall (whose
    header goes with it)."""
    rank = sorted(copies, key=lambda c: (c.total, c.ber, c.order))
    base = rank[0]
    if len(rank) == 1:
        return base.packet, base
    frames = [min(rank, key=lambda c: c.errors[i]).frames[i] for i in range(3)]
    center = min(rank, key=lambda c: c.center_err).center
    voice = (frames[0] << 144) | (frames[1] << 72) | frames[2]
    payload = ((voice >> 108) << 156) | (center << 108) | (voice & _MASK108)
    return base.packet[:20] + payload.to_bytes(33, 'big') + base.packet[53:], base


# ---- the vote ----

@dataclass
class Member:
    peer: bytes
    order: int
    stream_id: bytes
    joined: float
    last_t: float = 0.0
    last_pos: Optional[int] = None
    last_letter: int = 0
    lag: Optional[float] = None      # seconds behind the vote's timeline, at the least
    ended: bool = False
    used: int = 0                    # parts of the output that came from this peer


@dataclass
class Vote:
    key: Tuple
    hold_s: float
    started: float
    members: Dict[bytes, Member] = field(default_factory=dict)
    pending: Dict[int, List[Copy]] = field(default_factory=dict)
    first_in: Dict[int, float] = field(default_factory=dict)
    sent_voice: Dict[int, int] = field(default_factory=dict)      # position → voice bits sent
    next_out: Optional[int] = None
    t0: Optional[float] = None       # the timeline: position p0 came in at t0
    p0: int = 0
    header_peer: Optional[bytes] = None
    voice_out: bool = False
    end_at: Optional[int] = None     # a terminator came: positions from here on aren't sent
    terminator: Optional[bytes] = None
    end_t: float = 0.0
    done: bool = False
    last_activity: float = 0.0
    ctx: Any = None                  # HBProtocol's: the stream whose routing carries the call, …
    lost: int = 0                    # positions no peer delivered
    late: int = 0                    # copies that came after their position went out
    sent: int = 0

    @property
    def quiet_s(self) -> float:
        """A peer quiet this long has lost the call: its copies aren't waited for (each burst still
        goes out by its own deadline meanwhile; this only stops the wait on every burst)."""
        return max(QUIET_S, 2 * self.hold_s)

    # -- membership --

    def join(self, peer: bytes, stream_id: bytes, now: float) -> Member:
        m = self.members.get(peer)
        if m is not None:                         # the same peer, a new stream for the call
            m.stream_id, m.ended = stream_id, False
            return m
        m = Member(peer, len(self.members), stream_id, now, last_t=now)
        self.members[peer] = m
        self.last_activity = now
        return m

    def member_ended(self, peer: bytes) -> None:
        m = self.members.get(peer)
        if m is not None:
            m.ended = True

    # -- input --

    def add(self, peer: bytes, packet: bytes, frame_type: int, dtype_vseq: int, terminator: bool,
            now: float) -> List[Tuple[bytes, bool, bytes]]:
        """Take one packet of `peer`'s copy; return what to send now: (packet, ends the call, peer
        whose packet it is based on)."""
        m = self.members.get(peer)
        if m is None or self.done:
            return []
        self.last_activity = now
        if terminator:
            m.last_t, m.ended = now, True
            if self.end_at is None:            # right after its last burst, or the ones it lost since
                if m.last_pos is None:
                    self.end_at = self.next_out or 0
                else:
                    due = self.p0 + (now - self.t0 - (m.lag or 0.0)) / BURST_S
                    self.end_at = max(m.last_pos + 1, int(due + 0.5))
                self.terminator, self.end_t = packet, now
            return self.poll(now)
        letter = burst_letter(frame_type, dtype_vseq)
        if letter is None:                        # a header (or other data) before / between voice
            m.last_t = now
            if self.voice_out or self.end_at is not None:
                return []
            if self.header_peer is None:
                self.header_peer = peer
            return [(packet, False, peer)] if peer == self.header_peer else []
        pos = self._place(m, letter, packet, now)
        m.last_t, m.last_pos, m.last_letter = now, pos, letter
        if self.end_at is not None and pos >= self.end_at:
            return self.poll(now)
        if self.next_out is not None and pos < self.next_out:
            self.late += 1
            return self.poll(now)
        if self.next_out is None:
            self.next_out = pos
        self.pending.setdefault(pos, []).append(make_copy(peer, packet, letter, m.order))
        self.first_in.setdefault(pos, now)
        return self.poll(now)

    def _place(self, m: Member, letter: int, packet: bytes, now: float) -> int:
        """The position of `m`'s burst `letter` arriving at `now`."""
        if self.t0 is None:
            self.t0, self.p0 = now, letter
            m.lag = 0.0
            return letter
        lag = m.lag if m.lag is not None else 0.0
        expected = self.p0 + (now - self.t0 - lag) / BURST_S
        if m.last_pos is None:                    # a peer's first burst: near where the others are
            lo, hi = expected - 9, expected + 3
        else:
            lo, hi = m.last_pos + 1, max(expected + 1, m.last_pos + 1)
        first = int(lo) + ((letter - int(lo)) % SUPERFRAME)
        if first < lo:
            first += SUPERFRAME
        cands = list(range(first, int(hi) + 1, SUPERFRAME)) or [first]
        pos = self._by_content(cands, packet)
        if pos is None:
            pos = min(cands, key=lambda c: abs(c - expected)) if m.last_pos is None else cands[-1]
        seen = now - (self.t0 + (pos - self.p0) * BURST_S)
        m.lag = seen if m.lag is None else min(m.lag, seen)
        return pos

    def _by_content(self, cands: List[int], packet: bytes) -> Optional[int]:
        if not self.pending and not self.sent_voice:
            return None
        p = int.from_bytes(packet[20:53], 'big')
        voice = ((p >> 156) << 108) | (p & _MASK108)
        best, best_d = None, CONTENT_MATCH_BITS
        for c in cands:
            known = [x.voice for x in self.pending.get(c, [])]
            if c in self.sent_voice:
                known.append(self.sent_voice[c])
            for v in known:
                d = bin(v ^ voice).count('1')
                if d < best_d:
                    best, best_d = c, d
        return best

    # -- output --

    def _awaited(self, now: float) -> List[Member]:
        return [m for m in self.members.values()
                if not m.ended and m.last_pos is not None and now - m.last_t < self.quiet_s]

    def poll(self, now: float) -> List[Tuple[bytes, bool, bytes]]:
        """What's due by `now`."""
        out: List[Tuple[bytes, bool, bytes]] = []
        if self.done:
            return out
        waiting = self._awaited(now)
        while self.next_out is not None:
            p = self.next_out
            if self.end_at is not None and p >= self.end_at:
                break
            later = [q for q in self.pending if q >= p]
            if not later:
                break
            everyone = all(m.last_pos >= p for m in waiting)
            if p not in self.pending:
                q = min(later)
                if everyone or now >= self.first_in[q] + self.hold_s:
                    self.lost += 1
                    self.next_out = p + 1
                    continue
                break
            if not (everyone or now >= self.first_in[p] + self.hold_s):
                break
            copies = self.pending.pop(p)
            del self.first_in[p]
            packet, base = splice(copies)
            self._credit(copies)
            self.sent_voice[p] = make_copy(base.peer, packet, 0, 0).voice
            self.sent_voice.pop(p - HISTORY, None)
            self.voice_out = True
            self.sent += 1
            self.next_out = p + 1
            out.append((packet, False, base.peer))
        if self.end_at is not None and self._finished(now):
            self.done = True
            out.append((self.terminator, True, b''))
        return out

    def _finished(self, now: float) -> bool:
        """After a terminator: everything before it is out, or can't come any more (every peer
        still sending is past it, or the hold since the terminator is over)."""
        if any(q < self.end_at for q in self.pending):
            return False
        return (self.next_out is None or self.next_out >= self.end_at or now >= self.end_t + self.hold_s
                or all(m.last_pos >= self.end_at - 1 for m in self._awaited(now)))

    def _credit(self, copies: List[Copy]) -> None:
        if len(copies) == 1:
            self.members[copies[0].peer].used += 4
            return
        rank = sorted(copies, key=lambda c: (c.total, c.ber, c.order))
        for i in range(3):
            self.members[min(rank, key=lambda c: c.errors[i]).peer].used += 1
        self.members[min(rank, key=lambda c: c.center_err).peer].used += 1

    def next_deadline(self) -> Optional[float]:
        """When something comes due without waiting for more copies, or None."""
        if self.done:
            return None
        due = [min(self.first_in.values()) + self.hold_s] if self.first_in else []
        if self.end_at is not None:
            due.append(self.end_t + self.hold_s)
        return min(due) if due else None

    def summary(self) -> str:
        used = ', '.join(f'{int.from_bytes(m.peer, "big")}: {m.used}' for m in self.members.values())
        return (f'{len(self.members)} receivers, {self.sent} bursts sent, {self.lost} lost by all, '
                f'{self.late} late copies; parts from each ({used})')
