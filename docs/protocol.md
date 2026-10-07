# HomeBrew DMR Protocol Specification

This document describes the HomeBrew DMR protocol used for communication between DMR repeaters and servers *as implmented by this application*. There are extensions to HBP that are not used, and thus not documented. The primary source for information on HBP should be the MMDVMHost and DMRGateway code. While this code is poory commented and undocumented, it must still be considered primary. The protocol is based on UDP packets and implements a quasi connection-oriented approach with authentication and keepalive mechanisms.

> **Important Note**: The protocol specification at wiki.brandmeister.network contains errors in the keepalive/ping mechanism. Specifically, it reverses the command prefixes, incorrectly stating that MSTPING is sent from repeater to server and RPTPONG is sent back. The correct flow is: repeater sends RPTPING and server responds with MSTPONG. This document provides the correct implementation.

## Protocol Overview

The HomeBrew DMR protocol uses a series of commands exchanged between repeaters and servers to establish and maintain connections, authenticate devices, and transfer DMR data.

## Important DMR Specifications

- All Radio and Talkgroup IDs are 32-bit (4-byte) fields
- This matches the DMR over-the-air protocol where IDs map to IPv4 addresses
- Radio and Talkgroup IDs in all packets must be exactly 4 bytes, including any necessary leading zeros

## Connection States

A server will, at a minimum, need to track a repeater in the following states:
- `login` - Initial state after login request received
- `config` - Authentication completed, waiting for configuration
- `connected` - Fully connected and operational

## Command Types

### Connection Establishment

1. **RPTL (Repeater Login)**
   - Direction: Repeater → Server
   - Purpose: Initial login request
   - Format: `RPTL` + `radio_id[4 bytes]`
   - Note: Radio ID is always a 32-bit (4-byte) field as per DMR specification
   - State Change: `no` → `rptl-received`

2. **MSTNAK (Server NAK)**
   - Direction: Server → Repeater
   - Purpose: Reject a request/indicate an error
   - Used when: Invalid state transitions or authentication failures

3. **RPTK (Repeater Authentication)**
   - Direction: Repeater → Server
   - Format: `RPTK` + `radio_id[4 bytes]` + `authentication_response`
   - Purpose: Respond to authentication challenge
   - State Change: `rptl-received` → `waiting-config`

4. **RPTC (Repeater Configuration)**
   - Direction: Repeater → Server
   - Format: `RPTC` + `radio_id[4 bytes]` + `configuration_data`
   - Purpose: Send repeater configuration
   - State Change: `config` → `connected`
   - Total Length: 302 bytes
   
   Configuration Data Fields (all fields are fixed length):
   | Field         | Offset | Length | Description                  |
   |---------------|--------|--------|------------------------------|
   | Command       | 0      | 4      | 'RPTC'                       |
   | Radio ID      | 4      | 4      | 32-bit DMR ID                |
   | Callsign      | 8      | 8      | Station callsign             |
   | RX Frequency  | 16     | 9      | Receive frequency            |
   | TX Frequency  | 25     | 9      | Transmit frequency           |
   | TX Power      | 34     | 2      | Transmit power               |
   | Color Code    | 36     | 2      | DMR color code               |
   | Latitude      | 38     | 8      | Station latitude             |
   | Longitude     | 46     | 9      | Station longitude            |
   | Height        | 55     | 3      | Antenna height               |
   | Location      | 58     | 20     | Station location description |
   | Description   | 78     | 19     | Station description          |
   | Slots         | 97     | 1      | Enabled timeslots            |
   | URL           | 98     | 124    | Station URL                  |
   | Software ID   | 222    | 40     | Software identifier          |
   | Package ID    | 262    | 40     | Package identifier           |
   
   Note: All string fields are fixed length and should be null-padded if shorter than their allocated length.

5. **RPTO (Repeater Options)**
   - Direction: Repeater → Server
   - Format: `RPTO` + `radio_id[4 bytes]` + `options_string`
   - Purpose: Send talkgroup configuration for each timeslot
   - State: Must be in `connected` state
   - Server Response: `RPTACK` + `radio_id[4 bytes]`
   
   **Options String Format (basic):**
   ```
   TS1=tg1,tg2,tg3;TS2=tg4,tg5,tg6
   ```
   
   **Behavior:**
   - Repeater sends list of desired talkgroups for TS1 and TS2
   - Server performs intersection with configured allowed talkgroups
   - Only talkgroups present in both repeater request AND server config are accepted
   - Server has final authority (config is master allow list)
   - Updates repeater's active talkgroup lists
   - Can be sent at any time after connection established
   - Useful for dynamic talkgroup subscriptions
   
   **Example:**
   ```
   RPTO + [radio_id] + "TS1=1,2,3,91;TS2=10,99"
   ```
   
   If server config allows: TS1=[1,2,3,4,5] and TS2=[10,20,30]  
   Result: TS1=[1,2,3] (91 rejected), TS2=[10] (99 rejected)

   **Extended Options Grammar (trusted repeaters only):**

   HBlink4 accepts an extended form of each comma-separated entry to declare
   slot/talkgroup translation, plus a top-level `SRC=` directive that rewrites
   the rf_src on outgoing group-voice packets:

   ```
   entry          = net_tgid[:local_slot[:local_tgid]]
   net_tgid       = N | N-M           (range; inclusive; ≤10,000 tgids)
   local_slot     = 1 | 2 | *          (* = preserve network slot)
   local_tgid     = N | *              (* = preserve matched network tgid)

   SRC = radio_id                      (outbound rf_src override, group only)
   ```

   Wildcards are **not** permitted on the net-side (no `*`, no `N*` prefix).
   Most-specific rule wins on collision (exact=3 > range=2). See
   [dmrd_translation.md](dmrd_translation.md) for semantics, Link Control
   rewriting behavior, and use cases, and [connecting_to_hblink4.md](connecting_to_hblink4.md)
   for operator-facing `Options=` examples.

6. **RPTCL (Repeater Close)**
   - Direction: Repeater → Server
   - Format: `RPTCL` + `radio_id[4 bytes]`
   - Purpose: Graceful connection termination

### Connection Maintenance

1. **RPTPING (Repeater Ping)**
   - Direction: Repeater → Server
   - Format: `RPTPING` + `radio_id[4 bytes]`
   - Purpose: Repeater keepalive message
   - Notes: 
     - Sent periodically by repeater
     - Server responds with MSTPONG + radio_id
     - Updates last_ping timestamp
     - Resets missed_pings counter
     - Note: While repeaters always send 'RPTPING', only 'RPTP' is needed to identify the command when parsing,
       as these are the only significant characters needed to disambiguate it from other commands

2. **MSTPONG (Server Ping Response)**
   - Direction: Server → Repeater
   - Format: `MSTPONG` + `radio_id[4 bytes]`
   - Purpose: Acknowledge keepalive message from repeater
   - Usage:
     - Server sends in response to RPTPING/RPTP
     - Confirms server received keepalive message

3. **Timeout Behavior**
   - Repeater must send RPTPING/RPTP within the configured timeout period (default 30s)
   - Server tracks missed pings from each repeater
   - After max_missed pings (default 3), server considers repeater disconnected
   - Server will send NAK and remove repeater from active connections
   - Repeater must re-register if connection is lost

3. **RPTACK (Server Response)**
   - Direction: Server → Repeater
   - Format: `RPTACK` + `radio_id[4 bytes]`
   - Purpose: General acknowledgment for non-ping messages
   - Usage:
     - Server sends in response to various messages (except pings)
     - Confirms server received and accepted message

### Data Transfer

1. **DMRD (DMR Data)**
   - Direction: Bidirectional
   - Format: `DMRD` + `sequence[1 byte]` + `rf_src[3 bytes]` + `dst_id[3 bytes]` + `radio_id[4 bytes]` + `_bits[1 byte]` + `stream_id[4 bytes]` + `payload[33 bytes]`
   - Purpose: Transfer DMR voice/data packets
   - Notes: Only processed when connection state is 'connected'
   - Total Length: 53 bytes (4 + 1 + 3 + 3 + 4 + 1 + 4 + 33)

   **DMRD Packet Structure:**
   
   | Field       | Offset | Length | Description                       |
   |-------------|--------|--------|-----------------------------------|
   | Command     | 0      | 4      | 'DMRD'                            |
   | Sequence    | 4      | 1      | Packet sequence number (0-255)    |
   | RF Source   | 5      | 3      | Source radio ID (24-bit)          |
   | Destination | 8      | 3      | Destination talkgroup/ID (24-bit) |
   | Radio ID    | 11     | 4      | Repeater ID (32-bit)              |
   | _bits       | 15     | 1      | Control bits (see below)          |
   | Stream ID   | 16     | 4      | Unique stream identifier          |
   | Payload     | 20     | 33     | DMR voice/data payload            |

   **_bits Field (byte 15):**
   - Bit 7: Timeslot (0=Slot 1, 1=Slot 2)
   - Bit 6: Call Type (0=Group, 1=Private/Unit)
   - Bits 4-5: Frame Type
     - `00` - Voice frame
     - `01` - Voice Sync (header/terminator)
     - `10` - Data Sync (header/terminator)
     - `11` - Unused
   - Bits 0-3: Data Type / Voice Sequence (dtype_vseq)
     - For Data Sync frames: `0x1`=VHEAD (voice header with full LC), `0x2`=VTERM (voice terminator with full LC), `0x3`=CSBK, other values=data headers
     - For Voice / Voice Sync frames: `0x0`=burst A, `0x1`..`0x4`=bursts B..E (each carrying a 32-bit EMB_LC fragment), `0x5`=burst F

   **DMR Stream Terminator Detection:**
   
   In the Homebrew protocol, terminator frames are indicated in byte 15:
   - Bits 4-5 (frame type): Must be 0x2 (HBPF_DATA_SYNC)
   - Bits 0-3 (dtype_vseq): Must be 0x2 (HBPF_SLT_VTERM)
   
   ```python
   def _is_dmr_terminator(data: bytes, frame_type: int) -> bool:
       _bits = data[15]
       _dtype_vseq = _bits & 0xF
       return frame_type == 0x2 and _dtype_vseq == 0x2
   ```
   
   This provides ~60ms detection (3x faster than timeout methods).
   
   **Note**: ETSI sync patterns (bytes 20-25) are not used for terminator detection,
   as the Homebrew protocol provides explicit flags in the packet header.

### Roaming Transceivers (HBlink4 extension)

A roaming transceiver is a simplex radio that HBlink4 retunes for each unit call
it routes there (RadioDesk's `roaming_txvr`, driving an OpenGD77 in hotspot
mode). It registers with DMRC like MMDVMHost, under a pattern with
`roaming: true`; the RX/TX frequency in its DMRC keepalive is the channel it's
on now. Two packets are added:

1. **DMRT** — tune for a stream (master → peer, 19 bytes), sent before the
   stream's first DMRD:
   ```
   [0:4]   'DMRT'
   [4:8]   peer ID
   [8:12]  stream ID (the DMRD packets that follow carry it)
   [12:16] frequency, Hz, big-endian (simplex: RX = TX)
   [16]    color code
   [17]    power, 0-255 (MMDVM RF level; 255 = the radio's own setting)
   [18]    flags: 0x01 preempt — tune away even from a call it's hearing
           (HBlink4 decided to interrupt it; see `roaming_interrupt_rx`)
   ```
   Older transceivers read the first 18 bytes and ignore the flags.
2. **DMRK** — the answer (peer → master, 18 bytes), once the transceiver has
   tuned and listened before talking:
   ```
   [0:4]   'DMRK'
   [4:8]   peer ID
   [8:12]  stream ID
   [12]    status: 0 on air, 1 channel busy, 2 channel not allowed,
                   3 radio error, 4 transceiver busy (another call)
   [13:17] frequency, Hz, big-endian
   [17]    color code
   ```
   On anything but 0 the transceiver discards the stream and HBlink4 stops
   forwarding it there. Unless the channel was busy (it would be for any
   transceiver), HBlink4 sends DMRT to the next roamer and replays the stream
   from its start; with none left it emits `unit_call_unroutable` with the
   reason. No DMRK within `roaming_ack_timeout` (2 s) counts as a refusal.
   A transceiver that answered "radio error" or nothing is skipped for 30 s.

DMRD for a stream with no DMRT is dropped by the transceiver.

## Connection Flow

1. **Initial Connection**
   ```
   Repeater                   Server
      |                         |
      |---------- RPTL -------->| (Login Request)
      |                         |
      |<-------- MSTCL ---------|
      |                         |
      |---------- RPTK -------->| (Authentication)
      |                         |
      |---------- RPTC -------->| (Configuration)
      |                         |
      |<-------- RPTA ---------|| (Accept)
   ```

2. **Keepalive Flow**
   ```
   Repeater                   Server
      |                         |
      |-------- RPTPING ------->|
      |<------- MSTPONG --------|
      |                         |
   ```

## Timing and Reliability

- Repeaters must send MSTP messages regularly to maintain the connection
- If a repeater misses multiple pings, the connection is considered dead
- Server responds to each MSTP with RPTACK + radio_id
- Connection timeouts should be handled gracefully with RPTCL messages

## Error Handling

- Invalid packets or state transitions are responded to with MSTNAK
- Authentication failures result in connection termination
- Missing keepalive responses should trigger reconnection attempts
- Configuration errors should be logged and connection reset

## Security Considerations

- All repeaters must authenticate before sending data
- Radio IDs must be validated
- Configuration data should be validated before acceptance
- Connection states must be strictly enforced
- Implement rate limiting for login attempts

## Implementation Notes

- All messages are UDP packets
- Radio IDs are 4 bytes long
- String commands (like 'RPTL') are ASCII encoded
- Binary data (like DMR voice) is raw bytes
- Implement appropriate timeouts for all states
- Log all protocol messages when debugging
