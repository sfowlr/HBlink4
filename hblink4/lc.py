"""
DMR Link Control (LC) encode/splice helpers for the forwarding hot path.

When translation rewrites outbound addressing (TGID/slot/rf_src), the DMRD
header alone is not enough — MMDVMHost and end-radios also decode LC
embedded in the 33-byte DMR payload, and stale LC in that payload will
eventually contradict the rewritten header and wedge the stream.

Two LC carriers live in the payload:

  * FULL LC in Voice Header (VHEAD) / Voice Terminator (VTERM) data-sync
    frames — 196-bit BPTC(196,96) codeword spanning bits [0:98] and
    [166:264] of the payload; the 68-bit slot-type/sync pattern at
    [98:166] is preserved untouched (it carries colour-code + data-type,
    neither of which change under translation).
  * EMBEDDED LC fragment in voice superframe bursts B/C/D/E (dtype_vseq
    1..4 on voice frames) — 32 bits at [116:148]. A radio reassembles
    four consecutive fragments into the full LC for late entry + display.

Other payload slices (voice burst A, CSBKs, data headers) carry no LC,
so this module only touches the frames listed above. All FEC/interleave
math is delegated to dmr_utils3.

Hot path: encoding is done once per stream per target-addressing tuple
(see StreamState.lc_cache); splicing is per-packet but does nothing more
than a bitarray slice-concat-frombytes round trip.
"""

from typing import Dict, Optional, Tuple

from bitarray import bitarray
from dmr_utils3 import bptc, decode


# Default LC option bytes (FLCO=0 Group Voice, FID=0, service options=0x20).
# Matches hblink3's LC_OPT. Used when we can't snapshot the originator's LC
# from a VHEAD frame — e.g. late-entry streams where we see bursts before
# any data-sync frame, or recovery after a lost VHEAD.
LC_OPT_GROUP_DEFAULT: bytes = b'\x00\x00\x20'

# Full DMR payload is 264 bits (33 bytes). LC windows within it:
_PAYLOAD_BITS = 264
_FULL_LC_LOW = slice(0, 98)      # bits [0:98] — first half of 196-bit BPTC LC
_FULL_LC_SYNC = slice(98, 166)   # bits [98:166] — slot-type + sync pattern (preserved)
_FULL_LC_HIGH = slice(166, 264)  # bits [166:264] — second half of BPTC LC
_EMB_LC_WINDOW = slice(116, 148) # bits [116:148] — 32-bit EMB_LC fragment slot

# bptc.encode_header_lc / encode_terminator_lc produce 196-bit codewords.
# bptc.encode_emblc produces {1,2,3,4} → 32-bit bitarrays for bursts B..E.
_FULL_LC_BITS = 196

# Type aliases
FullLC = bitarray               # 196 bits
EmbLCSet = Dict[int, bitarray]  # {1,2,3,4} → 32-bit bitarray


def build_lc(opts: bytes, dst: bytes, src: bytes) -> bytes:
    """Assemble a 9-byte LC from 3-byte opts, dst, src.

    The LC wire format is: [3B FLCO/FID/service-options] [3B dst] [3B src].
    """
    return opts + dst + src


def synth_lc_base(dst: bytes, src: bytes,
                  opts: bytes = LC_OPT_GROUP_DEFAULT) -> bytes:
    """Build a synthesized 9-byte group-voice LC for a stream.

    Used when we can't decode the originator's VHEAD (late entry, missing
    header, private/unit call not yet supported). `opts` default gives
    FLCO=Group-Voice which is the only call type we currently forward.
    """
    return build_lc(opts, dst, src)


def decode_lc_from_vhead(payload: bytes) -> Optional[bytes]:
    """Extract the 9-byte LC from a Voice Header (VHEAD) payload.

    Called once per stream on a VHEAD frame so we can preserve the
    originator's FLCO / FID / service-option bits when we re-encode for
    translated targets. Returns None on any decode failure — callers fall
    back to a synthesized group-voice LC.
    """
    if len(payload) < 33:
        return None
    try:
        decoded = decode.voice_head_term(payload)
    except Exception:
        return None
    lc = decoded.get('LC') if decoded else None
    if isinstance(lc, (bytes, bytearray)) and len(lc) >= 9:
        return bytes(lc[:9])
    return None


def encode_lc_forms(lc: bytes) -> Tuple[FullLC, FullLC, EmbLCSet]:
    """Encode a 9-byte LC into every form the splicers need.

    Returns:
        h_lc:   196-bit BPTC codeword for VHEAD data-sync frames
        t_lc:   196-bit BPTC codeword for VTERM data-sync frames
        emb_lc: {1,2,3,4} → 32-bit fragments for voice bursts B/C/D/E
    """
    h_lc = bptc.encode_header_lc(lc)
    t_lc = bptc.encode_terminator_lc(lc)
    emb_lc = bptc.encode_emblc(lc)
    return h_lc, t_lc, emb_lc


def splice_full_lc(payload: bytes, full_lc: FullLC) -> bytes:
    """Return a new 33-byte payload with VHEAD/VTERM LC replaced.

    Keeps the 68-bit slot-type/sync window intact — colour code + data
    type (VHEAD vs VTERM) still decode correctly. Only the 196 LC bits
    change.
    """
    bits = bitarray(endian='big')
    bits.frombytes(payload)
    # full_lc is 196 bits; halves are [0:98] and [98:196].
    out = full_lc[0:98] + bits[_FULL_LC_SYNC] + full_lc[98:_FULL_LC_BITS]
    return out.tobytes()


def splice_emb_lc(payload: bytes, fragment: bitarray) -> bytes:
    """Return a new 33-byte payload with the 32-bit EMB_LC fragment replaced.

    AMBE vocoder bits at [0:116] and [148:264] are untouched so audio
    quality is preserved bit-for-bit.
    """
    bits = bitarray(endian='big')
    bits.frombytes(payload)
    out = bits[0:116] + fragment + bits[148:_PAYLOAD_BITS]
    return out.tobytes()


# Frame-type / dtype_vseq dispatch ------------------------------------------
#
# byte 15 encodes: bit7=slot, bit6=call_type, bits5-4=frame_type, bits3-0=dtype_vseq
# frame_type: 0 = voice, 1 = voice sync, 2 = data sync
# dtype_vseq when frame_type == 2:  1 = VHEAD, 2 = VTERM, 3 = CSBK, ... (data types)
# dtype_vseq when frame_type != 2:  0 = burst A, 1..4 = bursts B..E, 5 = burst F

# LC carrier kinds — what (if anything) the splicer should rewrite.
LC_CARRIER_NONE = 0
LC_CARRIER_VHEAD = 1
LC_CARRIER_VTERM = 2
LC_CARRIER_EMB = 3


def classify_lc_carrier(frame_type: int, dtype_vseq: int) -> int:
    """Identify which LC carrier (if any) lives in this payload.

    Returns one of LC_CARRIER_{NONE,VHEAD,VTERM,EMB}. Callers use this
    to decide whether to splice and which encoded form to use.
    """
    if frame_type == 2:
        if dtype_vseq == 1:
            return LC_CARRIER_VHEAD
        if dtype_vseq == 2:
            return LC_CARRIER_VTERM
        # CSBK (3) and other data types carry no LC we rewrite today.
        return LC_CARRIER_NONE
    # Voice / voice-sync frames: bursts B/C/D/E carry EMB_LC fragments.
    if 1 <= dtype_vseq <= 4:
        return LC_CARRIER_EMB
    return LC_CARRIER_NONE


# Stream kind classification ------------------------------------------------
#
# Distinguish voice calls from data calls by the first-packet frame type.
# The HBP `call_type` bit (byte 15 bit 6) only encodes group vs individual
# DMR addressing — it does NOT tell us voice vs data. Both voice and data
# calls can use either group or private addressing; the data-vs-voice split
# lives in the DMR payload's frame_type / dtype_vseq instead.
#
# Voice calls start on VHEAD (frame_type=2, dtype_vseq=1) and carry voice
# frames (frame_type 0 or 1) afterward. Data calls start on CSBK, a Data
# Header, or a data PDU frame — all of which use frame_type=2 with a
# dtype_vseq other than 1 (VHEAD) or 2 (VTERM).

STREAM_KIND_VOICE = 'voice'
STREAM_KIND_DATA = 'data'

# dtype_vseq names for log output. Meaningful only when frame_type == 2.
_DTYPE_NAMES: Dict[int, str] = {
    0: 'PI-Header',
    1: 'VHEAD',
    2: 'VTERM',
    3: 'CSBK',
    4: 'MBC-Header',
    5: 'MBC-Continuation',
    6: 'Data-Header',
    7: 'Rate-1/2 Data',
    8: 'Rate-3/4 Data',
    9: 'Idle',
    10: 'Rate-1 Data',
    11: 'Unified-Single-Block',
    12: 'Unified-Data-Cont',
}


def classify_stream_kind(frame_type: int, dtype_vseq: int) -> str:
    """Return STREAM_KIND_VOICE or STREAM_KIND_DATA for this packet.

    Only meaningful on the first packet of a stream — that's where the
    caller decides whether to track/forward as voice or log-and-drop as
    data. Mid-stream packets follow the classification set at stream start.
    """
    if frame_type == 2:
        if dtype_vseq == 1 or dtype_vseq == 2:
            return STREAM_KIND_VOICE
        return STREAM_KIND_DATA
    # frame_type 0 (voice) or 1 (voice sync). Late-entry voice streams can
    # arrive mid-superframe without a VHEAD — still voice.
    return STREAM_KIND_VOICE


def dtype_name(dtype_vseq: int) -> str:
    """Human-readable name for a dtype_vseq value (data-sync frames)."""
    return _DTYPE_NAMES.get(dtype_vseq, f'dtype-{dtype_vseq}')


# Data header decode -------------------------------------------------------
#
# DMR Data Header and CSBK frames ride in the same BPTC(196,96) codeword
# that voice VHEAD/VTERM use. dmr_utils3 already decodes the BPTC for us
# but truncates to the 72-bit (9-byte) voice-LC payload, dropping the RS
# parity tail. Data Header payloads are 96 bits (10 bytes data + 2 bytes
# CRC-CCITT) so we need to pull the full 96 bits from the info buffer.
#
# The 24 bits beyond decode_full_lc's output live at specific post-BPTC
# positions documented in dmr_utils3.bptc.decode_full_lc's source (the
# commented-out "RS1293 FEC we don't need" block). Order matters.

_DATA_HEADER_TAIL_POSITIONS: Tuple[int, ...] = (
    68, 53, 174, 159, 144, 129, 114, 99, 84, 69, 54, 39,
    24, 145, 130, 115, 100, 85, 70, 55, 40, 25, 10, 191,
)


# DPF (Data Packet Format) — byte 0 bits 5..0
_DPF_NAMES: Dict[int, str] = {
    0x0: 'UDT',
    0x1: 'Response',
    0x2: 'Unconfirmed-Data',
    0x3: 'Confirmed-Data',
    0xD: 'Short-Data-Defined',
    0xE: 'Short-Data-Raw',
    0xF: 'Proprietary',
}

# SAP (Service Access Point) — byte 1 bits 7..4
_SAP_NAMES: Dict[int, str] = {
    0x0: 'UDT',
    0x2: 'TCP/IP-HC',
    0x3: 'UDP/IP-HC',
    0x4: 'IP-Packet-Data',
    0x5: 'ARP',
    0x9: 'Proprietary',     # MotoTRBO XCMP / LRRP / APRS ride here
    0xA: 'Short-Data',
}


def dpf_name(dpf: int) -> str:
    return _DPF_NAMES.get(dpf, f'DPF-{dpf:#x}')


def sap_name(sap: int) -> str:
    return _SAP_NAMES.get(sap, f'SAP-{sap:#x}')


# BPTC(196,96) with its Hamming row (15,11) and column (13,9) codes corrected (ETSI TS 102 361-1
# Annex B.1.1; the same as RadioDesk's radiodesk.dmr.fec.bptc196_decode). Each code's parity checks:
# the data bits each parity bit covers.
_H15_11 = ((0, 1, 2, 3, 5, 7, 8), (1, 2, 3, 4, 6, 8, 9), (2, 3, 4, 5, 7, 9, 10), (0, 1, 2, 4, 6, 7, 10))
_H13_9 = ((0, 1, 3, 5, 6), (0, 1, 2, 4, 6, 7), (0, 1, 2, 3, 5, 7, 8), (0, 2, 4, 5, 8))
_BPTC_DATA_POS = (list(range(4, 12)) + list(range(16, 27)) + list(range(31, 42)) + list(range(46, 57))
                  + list(range(61, 72)) + list(range(76, 87)) + list(range(91, 102))
                  + list(range(106, 117)) + list(range(121, 132)))


def _syndrome(word: list, k: int, checks) -> int:
    s = 0
    for i, eq in enumerate(checks):
        p = word[k + i]
        for j in eq:
            p ^= word[j]
        if p:
            s |= 1 << i
    return s


def _syndrome_table(n: int, k: int, checks) -> dict:
    """Single-bit error position by syndrome."""
    table = {}
    for pos in range(n):
        w = [0] * n
        w[pos] = 1
        table[_syndrome(w, k, checks)] = pos
    return table


_FIX15 = _syndrome_table(15, 11, _H15_11)
_FIX13 = _syndrome_table(13, 9, _H13_9)


def _hamming_fix(word: list, k: int, checks, table: dict) -> bool:
    """Correct one bit error in `word` in place; True if a bit was flipped."""
    s = _syndrome(word, k, checks)
    if s and s in table:
        word[table[s]] ^= 1
        return True
    return False


def _decode_bptc_96(payload: bytes) -> Optional[bytes]:
    """Decode a 33-byte DMR data-sync payload → 12 bytes (96 bits), with
    single-bit errors per row and column corrected (iterated, as the codes
    are a product code). None if errors remain in a row after that.

    The 12 bytes are laid out per the transport that rides in this frame:
    for a Data Header, bytes 0-9 are the header and bytes 10-11 are the
    CRC-CCITT. For a CSBK, bytes 0-9 are the CSBK payload and bytes 10-11
    are the CRC.
    """
    if len(payload) < 33:
        return None
    bits = bitarray(endian='big')
    bits.frombytes(payload[:33])
    raw = (bits[0:98] + bits[166:264]).tolist()
    d = [raw[(a * 181) % 196] for a in range(196)]
    for _ in range(5):
        fixing = False
        for c in range(15):
            col = [d[c + 1 + 15 * r] for r in range(13)]
            if _hamming_fix(col, 9, _H13_9, _FIX13):
                for r in range(13):
                    d[c + 1 + 15 * r] = col[r]
                fixing = True
        for r in range(9):
            row = d[r * 15 + 1: r * 15 + 16]
            if _hamming_fix(row, 11, _H15_11, _FIX15):
                d[r * 15 + 1: r * 15 + 16] = row
                fixing = True
        if not fixing:
            break
    if any(_syndrome(d[r * 15 + 1: r * 15 + 16], 11, _H15_11) for r in range(9)):
        return None
    out = bitarray([d[p] for p in _BPTC_DATA_POS], endian='big')
    return out.tobytes()


def decode_bptc_block(payload: bytes) -> Optional[bytes]:
    """The 12 octets of any BPTC(196,96) data burst (a Rate 1/2 data block,
    e.g. a selective ACK's flags), or None on decode failure."""
    return _decode_bptc_96(payload)


CSBK_PREAMBLE = 0x3D
CSBK_CRC_MASK = 0xA5A5


def crc16_ccitt(data: bytes) -> int:
    """The CRC-CCITT of DMR headers and CSBKs (ETSI TS 102 361-1 B.3.7), before its mask."""
    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else (crc << 1)
            crc &= 0xFFFF
    return crc ^ 0xFFFF


def is_single_csbk(payload: bytes) -> bool:
    """True for a CSBK that is a whole transaction on its own: one that decodes
    with a good CRC and isn't a preamble CSBK (opcode 0x3D, which announces a
    data header and blocks to follow). A radio check, call alert, emergency
    alarm or their answers are one burst with no header or terminator. Octet 0:
    last block, protect flag, opcode (6 bits); octets 10-11: the CRC, masked."""
    raw = _decode_bptc_96(payload)
    if raw is None or len(raw) < 12 or (raw[0] & 0x3F) == CSBK_PREAMBLE:
        return False
    return crc16_ccitt(raw[:10]) ^ CSBK_CRC_MASK == int.from_bytes(raw[10:12], 'big')


def decode_data_header(payload: bytes) -> Optional[Dict[str, object]]:
    """Extract identifying fields from a DMR Data Header payload.

    Args:
        payload: 33-byte DMR payload from a frame_type=2 / dtype_vseq=6
                 packet (Data Header).

    Returns:
        Dict with keys: group (bool), response_requested (bool),
        dpf (int), dpf_name (str), sap (int), sap_name (str),
        blocks_to_follow (int) — octet 8 bits 6-0 for Response, Unconfirmed
        and Confirmed Data Headers (DPF 1, 2, 3), else 0 — and raw (12-byte
        payload).
        Returns None on decode failure.

    Does not verify the CRC-CCITT tail — callers that need confidence can
    check raw[-2:] against their own CRC implementation. The decoded fields
    come from bytes 0-1 and 8.
    """
    raw = _decode_bptc_96(payload)
    if raw is None or len(raw) < 12:
        return None
    b0 = raw[0]
    b1 = raw[1]
    dpf = b0 & 0x0F
    return {
        'group': bool(b0 & 0x80),
        'response_requested': bool(b0 & 0x40),
        'dpf': dpf,
        'dpf_name': dpf_name(dpf),
        'sap': (b1 >> 4) & 0x0F,
        'sap_name': sap_name((b1 >> 4) & 0x0F),
        'blocks_to_follow': raw[8] & 0x7F if dpf in (1, 2, 3) else 0,
        'raw': raw,
    }
