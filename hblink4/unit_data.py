"""
Unit data delivery: what became of each unit (private) data packet HBlink4
forwards (`global.forward_unit_data`), and optional retries.

A confirmed data packet (ETSI TS 102 361-1 §8.2.1.2: DPF 3 with the A bit,
"response requested") is answered by the radio with a response packet
(§8.2.2.3, figure 8.5: DPF 1), which HBlink4 forwards back to the sender like
any unit data. HBlink4 matches the response to the packet it answers by the
addresses swapped (the response goes from the packet's destination to its
source) and timing (after the packet, within
`global.unit_data_response_timeout`), and reports on the packet's stream:

    delivered    ACK (class 00, type 001)
    nacked       NACK (class 01: `reason` from table 8.3), or selective ACK
                 (class 10: `missing`, the blocks the radio asks for again)
    no_response  nothing within the timeout

Response header, octet 9 (figure 8.5): class (2 bits), type (3), status (3:
NI, the N(S) of the last packet received). A selective ACK is followed by
Rate 1/2 blocks of flags (figure 8.17): bit j of octet k is block 8k + j,
1 = received, 0 = send it again, then the packet CRC-32. OpenGD77's ACK
(firmware dmrData.c, dmrDataBuildResponseAck) and DSD-FME's decoder
(src/dmr_block.c) read the header the same way.

`global.unit_data_retry` ({"enabled": false, "attempts": 2, "wait_s": 10.0,
"busy_wait_s": 120.0}): for senders with no retry logic of their own, HBlink4
keeps each unit data packet it routes and, `wait_s` after a no_response, a NACK
worth resending (packet CRC, memory full), a selective ACK or a retryable
failure (no route …), sends the whole packet again — routed afresh, as a new
stream — up to `attempts` more times, then reports the final outcome.

A busy destination (BUSY_REASONS: its slot in a call or hang time, its channel
busy, no roaming transceiver free) isn't a failed attempt: the packet waits,
routed again every BUSY_POLL_S, and goes as soon as there's room — for up to
`busy_wait_s`, then it fails.
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional

DPF_RESPONSE, DPF_UNCONFIRMED, DPF_CONFIRMED = 1, 2, 3

RESPONSE_ACK, RESPONSE_NACK, RESPONSE_SACK = 0, 1, 2
NACK_REASONS = {
    0: 'illegal format',
    1: 'packet CRC failed',
    2: 'memory full',
    3: 'fragment out of sequence',
    4: 'undeliverable',
    5: 'packet out of sequence',
    6: 'invalid user',
}
# NACK types where sending the same packet again can help.
NACK_RETRY = (1, 2)
# Failures retrying won't fix: the source isn't allowed to send unit data.
NO_RETRY_REASONS = ('unit calls not enabled', 'unit data not forwarded')
# The destination is busy, not unreachable: wait for it, without using up an attempt.
BUSY_REASONS = ('slot busy', 'slot in hang time', 'channel busy', 'transceiver busy', 'no roaming transceiver free')
BUSY_POLL_S = 0.5

MAX_KEPT_PACKETS = 200          # preambles + header + 127 blocks, with room


@dataclass
class Response:
    cls: int
    typ: int
    status: int
    blocks: int

    @property
    def outcome(self) -> str:
        return 'delivered' if (self.cls, self.typ) == (RESPONSE_ACK, 1) else 'nacked'

    @property
    def kind(self) -> str:
        return {RESPONSE_ACK: 'ack', RESPONSE_NACK: 'nack', RESPONSE_SACK: 'sack'}.get(self.cls, 'unknown')

    @property
    def reason(self) -> Optional[str]:
        if self.cls == RESPONSE_NACK:
            return NACK_REASONS.get(self.typ, f'NACK type {self.typ}')
        if self.cls == RESPONSE_SACK:
            return 'selective retry'
        if self.outcome != 'delivered':
            return f'response class {self.cls} type {self.typ}'
        return None

    @property
    def worth_retrying(self) -> bool:
        return self.cls == RESPONSE_SACK or (self.cls == RESPONSE_NACK and self.typ in NACK_RETRY)


def parse_response(raw12: bytes) -> Optional[Response]:
    """A response packet header (12 octets after BPTC), or None if it isn't one."""
    if len(raw12) < 10 or raw12[0] & 0x0F != DPF_RESPONSE:
        return None
    return Response(cls=raw12[9] >> 6, typ=(raw12[9] >> 3) & 7, status=raw12[9] & 7, blocks=raw12[8] & 0x7F)


def sack_missing(flags: bytes, blocks: Optional[int] = None) -> List[int]:
    """The blocks a selective ACK's flags ask for again (of the first `blocks`, if known)."""
    n = len(flags) * 8 if blocks is None else min(blocks, len(flags) * 8)
    return [i for i in range(n) if not (flags[i // 8] >> (i % 8)) & 1]


@dataclass
class UnitDataTx:
    """One unit data packet HBlink4 routed (or tried to), until its outcome is known."""
    stream_id: bytes                    # the stream it arrived as: statuses are reported under it
    rf_src: bytes
    dst_id: bytes
    source_rid: bytes                   # the local peer it came from
    slot: int
    started: float
    last_seen: float
    packets: Optional[List[bytes]] = None   # kept for retries (global.unit_data_retry)
    confirmed: bool = False             # DPF 3 with a response requested
    ns: Optional[int] = None
    blocks: Optional[int] = None        # blocks to follow in its header
    attempt: int = 1
    current_sid: Optional[bytes] = None  # the stream id of the attempt on the air (a retry's is new)
    completed_at: Optional[float] = None
    deadline: Optional[float] = None    # a response is due by then
    retry_at: Optional[float] = None
    busy_since: Optional[float] = None  # waiting for a busy destination since (BUSY_REASONS)
    busy_retry: bool = False            # the next send is after a busy wait: not a new attempt
    failed_reason: Optional[str] = None
    done: bool = False
    extra: Dict[str, object] = field(default_factory=dict)

    def __post_init__(self):
        if self.current_sid is None:
            self.current_sid = self.stream_id
