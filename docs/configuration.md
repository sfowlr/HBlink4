# HBlink4 Configuration Guide

HBlink4 uses a JSON configuration file (`config/config.json`) to define server settings, repeater access control rules, and talkgroup routing. This guide explains each configuration section and its options.

## Configuration File Structure

The HBlink4 server configuration file consists of five main sections:
- **Global Settings** - Server-wide settings
- **Dashboard** - Web dashboard and event communication
- **Blacklist Rules** - Access control for blocking repeaters
- **Repeater Configurations** - Per-repeater authentication and routing
- **Network Outbound** - Server-to-server links (optional)

## Global Settings

The `global` section contains server-wide settings that control the basic operation of HBlink4.

```json
{
    "global": {
        "max_missed": 3,
        "timeout_duration": 30,
        "disable_ipv6": false,
        "bind_ipv4": "0.0.0.0",
        "bind_ipv6": "::",
        "port_ipv4": 62031,
        "port_ipv6": 62031,
        "logging": {
            "file": "logs/hblink.log",
            "console_level": "INFO",
            "file_level": "DEBUG",
            "retention_days": 30
        },
        "stream_timeout": 2.0,
        "stream_hang_time": 10.0,
        "user_cache": {
            "timeout": 600
        },
        "sctp_enabled": false,
        "sctp_port_ipv4": 62031,
        "sctp_port_ipv6": 62031
    }
}
```

| Setting | Type | Description |
|---------|------|-------------|
| `max_missed` | number | Maximum consecutive missed pings before disconnecting a repeater (default: 3) |
| `timeout_duration` | number | Seconds between expected pings from repeaters (default: 30) |
| `disable_ipv6` | boolean | **Disable IPv6 globally** - use only if your network has broken IPv6 routing (default: false) |
| `bind_ipv4` | string | IPv4 address to bind ("0.0.0.0" for all IPv4 interfaces) |
| `bind_ipv6` | string | IPv6 address to bind ("::" for all IPv6 interfaces) |
| `port_ipv4` | number | UDP port for IPv4 (default: 62031) |
| `port_ipv6` | number | UDP port for IPv6 (default: 62031) |
| `sctp_enabled` | boolean | Enable SCTP listeners alongside UDP (default: false). Requires Linux kernel SCTP support (`modprobe sctp`). Falls back to UDP-only if unavailable. |
| `sctp_port_ipv4` | number | SCTP port for IPv4 (default: same as `port_ipv4`). Can be the same as the UDP port since they're different protocols. |
| `sctp_port_ipv6` | number | SCTP port for IPv6 (default: same as `port_ipv6`) |
| `logging.file` | string | Path to log file |
| `logging.console_level` | string | Logging level for console output ("DEBUG", "INFO", "WARNING", "ERROR") |
| `logging.file_level` | string | Logging level for file output ("DEBUG", "INFO", "WARNING", "ERROR") |
| `logging.retention_days` | number | Number of days to retain log files (default: 30) |
| `stream_timeout` | float | Fallback timeout when terminator frame is lost (default: 2.0 seconds) |
| `stream_hang_time` | float | Seconds to reserve slot for same source after stream ends (default: 10.0-20.0 seconds) |
| `user_cache.timeout` | number | Seconds before user cache entries expire (default: 600, minimum: 60) |
| `unit_call_flood` | boolean | Broadcast unit calls whose target can't be located to every unit-enabled, TX-capable peer (default: `false` — such calls are not sent) |
| `forward_unit_data` | boolean | Forward unit (private) data calls — IP/UDP, text messages, any data header and blocks, and single CSBKs such as a radio check or call alert and the radio's answer — to the target radio like unit voice calls (same routing, same `unit_calls_enabled` gate on the source peer). — except unit data to a pinned ID (`static_subscribers`: a bot or gateway that owns it), which is forwarded from any peer, a receive-only SDR included: a radio's reply to it may be heard only there. The same unit data burst heard by two peers within a second (an SDR and a roamer on one channel) is forwarded once Default `false`: data calls are logged, not forwarded. Group data is never forwarded |
| `static_subscribers` | object | `{"<radio_id>": <repeater_id>}`: always route unit calls for these radios to that peer, e.g. a dispatch console or gateway peer that owns those IDs |
| `roaming_channels` | array | Channels a roaming transceiver may be tuned to: `[{"freq": "462.5625", "cc": 1, "power": 128, "tg": 201}]` (`freq` in MHz or Hz; `cc` used when the radio's isn't known; `power` 0-255 MMDVM RF level, default 255 = the radio's own setting; `tg`, optional: the channel a group call to that talkgroup from the network goes out on). Simplex channels only |
| `roaming_group_calls` | string | Default for roamers without their own: may a roamer carry group calls to its site (see "Sites")? `"all"` (any roaming channel), `"idle"` (only its idle channel) or `"none"` (default) |
| `site_radius_km` | float | Two peers (or a peer and where a radio was heard) without site names on both, but with locations, are one site within this distance (default: 20). See "Sites" |
| `roaming_interrupt_rx` | string | Default for roamers without their own: may a roamer hearing a call on its channel be pulled off it to transmit elsewhere? `"never"` (default), `"last_resort"` (only when no other roamer is free) or `"as_needed"` (it competes on priority like a free one) |
| `roaming_ack_timeout` | float | Seconds to wait for a roamer's DMRK before handing the call to the next one (default: 2.0) |
| `unit_data_response_timeout` | float | Seconds after a confirmed unit data packet (one asking for a response) has gone out to wait for the radio's response packet before reporting `no_response` (default: 5.0). See "Unit data delivery" |
| `unit_data_retry` | object | `{"enabled": false, "attempts": 2, "wait_s": 10.0, "busy_wait_s": 120.0}`: when enabled, HBlink4 keeps each unit data packet it routes and sends it again, whole, `wait_s` after a `no_response`, a NACK worth resending, a selective ACK or a retryable failure, up to `attempts` more times. A busy destination isn't an attempt: the packet waits until the slot is free (up to `busy_wait_s`), then goes. Off by default; leave it off for senders that retry themselves (see "Unit data delivery") |
| `voting` | object | `{"enabled": false, "hold_ms": 150}`: receiver voting. A voice call heard by several peers goes out once, built burst by burst from the best copy of each part (see "Receiver voting"). Off by default: the first peer's copy goes out and the others are dropped as contention |
| `external_last_heard` | object | Optional MQTT feed of where radios are listening, from systems outside HBlink4 (receivers that aren't peers, ARS registrations …): `{"host", "port", "username", "password", "topic": "hblink4/last_heard", "tls", "status_topic"}`. Message format in `hblink4/external_last_heard.py`. With `status_topic` (e.g. `"hblink4/unit_call"`), each unit call's outcome is published on the same connection to `{status_topic}/{source radio id}` (see below), so its sender can try again. Needs `paho-mqtt` |

**Note on IPv6**: HBlink4 is dual-stack native and will bind to both IPv4 and IPv6 by default. If your network appears to support IPv6 but connections don't establish properly (a common issue with misconfigured IPv6), set `disable_ipv6: true` to force IPv4-only mode.

**User Cache**: The user cache tracks the last known repeater for each DMR ID to enable efficient private call routing. Entries are automatically cleaned up every 60 seconds. The timeout must be at least 60 seconds.

**Unit call routing**: a unit (private) call goes only to where its target radio is listening, on the target's last-heard slot (not the caller's):

1. the peer the radio was last heard on, if that peer can transmit;
2. otherwise a TX-capable, unit-enabled peer on the same channel — its RX or TX frequency (from RPTC/DMRC) matching the frequency of the peer the radio was heard on, and the same color code when both are known. This covers radios heard by receive-only peers (pattern `tx: false`, e.g. SDR receivers), and radios reported by `external_last_heard` (the newest report wins, whether heard here or reported);
3. otherwise, when **no** fixed TX peer is on that channel (busy or not — two transmitters on one simplex frequency would collide), the channel is in `roaming_channels`, and no peer is hearing traffic on it right now: a roaming transceiver (pattern `roaming: true`), retuned to it, on TS2. Which one: one already idling on that channel always; otherwise the lowest `roaming_priority`, equal priorities taking turns (least recently used); one hearing a call only as its `roaming_interrupt_rx` allows. If it hears the channel busy, the call is dropped (`unit_call_unroutable` with a `reason`); if it refuses for its own reasons (busy, radio error, no answer), the next roamer gets the call, replayed from the start;
4. otherwise the call is not sent (logged `[no route]`, dashboard event `unit_call_unroutable`), unless `unit_call_flood` is set.

Steps 2 and 3 use only peers at the radio's site, and "hearing traffic on it" means there (see "Sites").

**Sites**: one channel plan can be in use at several places too far apart to hear each other, e.g. two receiver sites feeding one server. HBlink4 then has to know which peers are where:

- A peer's site is its pattern's `site` (any name). Two places are one site when both have a name and the names match; otherwise when both have a location (RPTC, DMRC tail) and are within `site_radius_km`; otherwise (unknown) they count as one site, so a network in one place needs neither.
- A radio's site is that of the peer it was heard on, or the `site` / `latitude` / `longitude` in an `external_last_heard` report. A report with neither keeps the site already known for the radio on that channel. Peers that don't send a location (or a gateway whose receivers don't know theirs) get one from a `site` in their patterns.
- **Unit calls** go out only at the radio's site: a fixed peer or roamer elsewhere is never used, and traffic elsewhere doesn't make the channel busy. No free transmitter there: the call fails (`no roaming transceiver free`).
- **Group calls** heard on the air at one site are not sent to a peer transmitting on the talker's channel at that site (simplex: the talker is still on it). Roaming transceivers carry them to the other sites, one roamer per site, on the same channel, as their `roaming_group_calls` allows, and only where no fixed peer is on that channel and nothing is heard on it. A group call from the network (no channel: a console, a bot) goes out on the roaming channel whose `tg` is its talkgroup, at every site. A roamer that refuses (busy, radio error, no answer) hands it to another at the same site. A roamer busy with a group call can't take a unit call until it's done.
- Our own transmission heard back by a receiver at the site we're sending it at (same source and talkgroup, during the call or its hang time) is not forwarded and doesn't move the radio in the last-heard list.

Each unit call's outcome is a `unit_call_status` event, and with `external_last_heard.status_topic` an MQTT message at `{status_topic}/{src_id}`: `{"stream_id": "aabbccdd", "src_id", "dst_id", "status", "reason", "at"}`. `status` is `routed` (forwarded to a fixed peer or an outbound, or broadcast), `on_air` (a roaming transceiver answered DMRK on air; with `repeater_id`, `freq`, `colorcode`) or `failed` (`reason`: `no route`, `channel busy`, `channel not allowed`, `no roaming transceiver free`, a roamer's refusal, `slot in hang time`, `unit calls not enabled`). Each status is sent once per stream. HBlink4 doesn't retry a failed call; the sender does (RadioDesk's bot re-sends its replies).

**Unit data delivery**: with `forward_unit_data`, unit data packets get the same reports, with `"is_data": true`: `routed` / `on_air` / `failed` as above (plus `failed` with `unit data not forwarded` when `forward_unit_data` is off), under the stream id of the packet's first burst. A single CSBK that isn't a preamble (a radio check, call alert, emergency alarm, or the radio's answer to one; good CRC) is a whole transaction in one burst: its stream ends there, so the answer can come straight back on the same slots, and it gets `routed` / `on_air` / `failed` only (it asks for no data response). It's never held or sent again by `unit_data_retry`: its sender asks again itself. Our own unit call or unit data heard back by another peer listening on the frequency a peer at that site is sending it on (a receiver hearing the roamer, say) is an echo: not routed, and not taken as where its source is, so an answer to that source still goes back to the peer that sent it. The same goes for `external_last_heard` reports: one placing a source ID on the frequency HBlink4 was transmitting as that ID on, at that time (within a minute's memory, ±3 s), is our own transmission heard by a receiver outside HBlink4, and is ignored. Data and control bursts (CSBKs, data headers) are decoded with BPTC(196,96)'s Hamming codes corrected, so a burst with a bit error or two is still read right. A **confirmed** packet (ETSI TS 102 361-1 §8.2.1.2: DPF 3 with the A bit, "response requested") is answered by the radio with a response packet (DPF 1), which HBlink4 forwards back to the sender as usual and matches to the packet it answers — the response goes from the packet's destination to its source, after the packet and within `unit_data_response_timeout` — then reports one more status for the packet:

| `status` | When | Extra fields |
|---|---|---|
| `delivered` | the radio's ACK (class 00, type 001) | `response: "ack"`, `ns` (the response's status: the N(S) it acknowledges) |
| `nacked` | a NACK (class 01) or a selective ACK (class 10) | `response: "nack"` with `reason` (table 8.3: `packet CRC failed`, `memory full`, `undeliverable`, `packet out of sequence`, `invalid user`, `illegal format`, `fragment out of sequence`), or `response: "sack"`, `reason: "selective retry"` and `missing`: the blocks (serial numbers) the radio asks for again |
| `no_response` | nothing within `unit_data_response_timeout` (or `superseded by a newer packet` from the same source to the same radio) | |
| `waiting` | not final: with `unit_data_retry`, the destination is busy and HBlink4 is holding the packet for room (see "Retrying unit data") | `reason` (`slot busy` …), `busy_wait_s` |

The response packet itself isn't reported. An unconfirmed packet's last status is `routed` (or `on_air` / `failed`): nothing comes back to wait for.

**Retrying unit data** (`unit_data_retry`, off by default) is for senders with no retry logic of their own. HBlink4 keeps each unit data packet it routes and, `wait_s` after a `no_response`, a NACK worth resending (`packet CRC failed`, `memory full`), a selective ACK, or a failure retrying can fix (`no route` …, not `unit calls not enabled`), sends the whole packet again: routed afresh (the radio may have moved), as a new stream, one burst per 60 ms. Up to `attempts` more times; meanwhile the negative outcomes aren't published, each new attempt's `routed` is (with `"attempt": n`), and the last outcome is the final one (`delivered` / `nacked` / `no_response` / `failed`, with `attempt`). Statuses stay under the original stream id.

A **busy destination** doesn't use up an attempt: when the packet can't go because the radio's slot is in another call or its hang time (`slot busy`, `slot in hang time`), its channel is busy (`channel busy`, `transceiver busy`) or no roaming transceiver is free, HBlink4 holds it, reports `waiting` (with the reason and `busy_wait_s`; not a final status), and routes it again every half second, sending it the moment there's room — then waits for the response as usual. After `busy_wait_s` (120 s) of waiting it's `failed`, with `slot busy for 120 s` (or the like). A newer packet from the same source to the same radio (the sender's own retry) replaces one that's waiting: the waiting one ends `no_response`, `superseded by a newer packet`. Without `unit_data_retry`, a busy destination fails at once with its reason (`slot busy` …).

A sender that retries itself — RadioDesk's bot, which sends text messages as confirmed data and resends from the radio's response — should leave this off:

- **Double delivery.** When the radio's ACK is lost on the way back, HBlink4 sends the packet again though the radio has it. A radio that follows ETSI drops a confirmed duplicate by its N(S); one that doesn't, and every unconfirmed packet (a text sent unconfirmed, a Motorola TMS), shows the message twice. With both retrying, each lost response can mean two more copies.
- **The sender knows better.** It sees the response: a selective ACK names the blocks to send again, an out-of-sequence NACK the N(S) to use, and it marks retries as such (the F bit). HBlink4 can only resend the packet as it was.

**Receiver voting** (`voting`, off by default) is for one transmission heard by several peers: receivers at one site where terrain splits the coverage, a repeater and an SDR on its input, or receivers at different sites. Without it, the first peer's copy is forwarded and every other copy is refused as contention, gaps and all. With it, the first peer's stream opens a vote and carries the call (its routing, its stream id); another peer's stream of the same call (the same source to the same network destination, group or unit, while the vote has heard something in the last second) joins the vote instead of being routed on its own. Every copy's packets go through the vote, which sends each burst once:

- **Line-up.** A voice burst's letter (A–F) places it within a superframe; the arrival time, less that peer's latency, picks the superframe, or a copy of the same burst already in (the same AMBE bits) when a peer is far behind.
- **Waiting.** A burst goes out once every peer still sending has delivered it, so with one peer there's no delay at all, or `hold_ms` after its first copy came in. A peer quiet for half a second (or two holds) has lost the call and isn't waited for. A burst no peer delivered is skipped.
- **Best copy.** Of a burst's copies, each of its three AMBE frames comes from the copy with the fewest FEC errors in that frame (Golay C0 and C1; an uncorrectable C0 counts 8). The sync or EMB in the middle comes from the copy where it's cleanest. Ties go to the copy best overall, then the lower BER the peer reported, then the peer that joined first.
- **Headers and the end.** The voice headers come from the first peer that sent one. A terminator from any peer ends the call once the bursts before it are out. Copies of an already-sent burst ("late") are dropped.
- **Routing.** A joining peer isn't sent the call: a repeater repeats its own copy. Neither is a peer at its site that transmits on the channel it hears the talker on. A peer we were already sending the call to that hears the talker itself joins the vote too. Our own transmission heard back (an echo, below) never joins.

When a vote with more than one peer ends, the log has a line with how many bursts went out, how many every peer lost, how many copies came late, and how many parts of the call came from each peer. `hold_ms` adds that much delay, at most, and only once a second peer is in the call: keep it above the difference in latency between your receivers.

A group call heard by a receiver while HBlink4 sends the same source and talkgroup out a peer at that receiver's site is an echo of our own transmission only when that peer transmits on the frequency the receiver listens on (or is a roamer, which only carries a call away from the talker's site); a peer with no frequency (a console, a bot) can't be heard over the air.

> ⚠️ **Don't set `user_cache.timeout` shorter than your longest expected transmission.** A cache entry's `last_heard` is refreshed only at stream start (PTT), not on every voice packet, so a transmission that outlasts the timeout will leave the talker's entry expired by the time they unkey — even though they were clearly active the whole time. DMR transmissions can run 2–3 minutes, so keep the timeout well above that. The 600-second default comfortably covers normal operation; the 60-second minimum is permitted but only appropriate for testing or very short-TX environments. Setting it too low degrades unit-call routing by forcing unnecessary broadcasts immediately after long transmissions.

### Dual-Stack IPv6 Support

HBlink4 is **dual-stack native** and can listen on both IPv4 and IPv6 simultaneously:

- Set `bind_ipv4` to `"0.0.0.0"` to listen on all IPv4 interfaces
- Set `bind_ipv6` to `"::"` to listen on all IPv6 interfaces
- Both can be active simultaneously for maximum compatibility
- Specific addresses can be used instead of wildcards (e.g., `"192.168.1.10"` or `"2001:db8::1"`)
- Use `disable_ipv6: true` to force IPv4-only mode if IPv6 is broken on your network

**Common Issue: "Address Already in Use" on IPv6 Bind**

If you see an error like "address already in use" when binding IPv6 with the same port as IPv4, your system's IPv6 stack is in dual-stack mode (IPv6 can handle both IPv4 and IPv6 on the same port). This is **normal and expected** on many Linux systems.

**Solutions:**
1. **Use different ports** (simple): `port_ipv4: 62031`, `port_ipv6: 62032`
2. **Disable IPv6** (IPv4-only): Set `disable_ipv6: true`
3. **Let IPv6 handle both** (advanced): Set `bind_ipv4: ""` to disable IPv4 bind

**Example configurations:**
```json
// Dual-stack with separate ports (RECOMMENDED if you see bind errors)
"bind_ipv4": "0.0.0.0",
"bind_ipv6": "::",
"port_ipv4": 62031,
"port_ipv6": 62032,

// IPv4 only (simple and reliable)
"disable_ipv6": true,
"bind_ipv4": "0.0.0.0",
"port_ipv4": 62031,

// Specific addresses (no port conflict)
"bind_ipv4": "192.168.1.10",
"bind_ipv6": "2001:db8::1",
"port_ipv4": 62031,
"port_ipv6": 62031
```

### SCTP Transport (Optional)

HBlink4 can optionally listen for inbound connections via **SCTP** (Stream Control Transmission Protocol) alongside the default UDP listeners. SCTP provides:

- **Message boundaries preserved** (like UDP) — no framing or reassembly needed
- **Connection-oriented** (like TCP) — connection_made/connection_lost lifecycle
- **Built-in heartbeat detection** — kernel detects dead peers automatically
- **SCTP_NODELAY** always enabled — no Nagle buffering of small DMR packets

**Requirements:**
- Linux kernel with SCTP support: `sudo modprobe sctp`
- macOS does **not** support SCTP — the server logs a warning and continues UDP-only
- The connecting client (MMDVMHost) must also support SCTP

**Configuration:**
```json
{
    "global": {
        "sctp_enabled": true,
        "sctp_port_ipv4": 62031,
        "sctp_port_ipv6": 62031
    }
}
```

SCTP and UDP listeners run simultaneously — repeaters can connect via either protocol. SCTP ports can be the same as UDP ports since they are different protocols and don't conflict.

For outbound connections, set `"transport": "sctp"` on the individual connection:
```json
{
    "outbound_connections": [
        {
            "name": "Master-Server",
            "address": "master.example.com",
            "port": 62031,
            "transport": "sctp",
            ...
        }
    ]
}
```

> ℹ️ **Application-level keepalives (RPTPING/MSTPONG) are still used with SCTP.** The HomeBrew protocol state machine requires them regardless of transport. SCTP heartbeats provide an additional layer of dead-peer detection at the kernel level.

## Dashboard Configuration

The `dashboard` section is a **top-level** configuration (not nested under `global`) and controls the real-time monitoring dashboard and event communication:

```json
{
    "dashboard": {
        "enabled": true,
        "disable_ipv6": false,
        "transport": "unix",
        "host_ipv4": "127.0.0.1",
        "host_ipv6": "::1",
        "port": 8765,
        "unix_socket": "/tmp/hblink4.sock",
        "buffer_size": 65536
    }
}
```

| Setting | Type | Description |
|---------|------|-------------|
| `enabled` | boolean | Enable/disable dashboard event emitting |
| `disable_ipv6` | boolean | Disable IPv6 for dashboard (independent of global setting) |
| `transport` | string | Transport type: `"unix"` or `"tcp"` (see below) |
| `host_ipv4` | string | IPv4 address for TCP transport (e.g., "127.0.0.1") |
| `host_ipv6` | string | IPv6 address for TCP transport (e.g., "::1") |
| `port` | number | Port number for TCP transport (default: 8765) |
| `unix_socket` | string | Unix socket path for Unix transport (default: "/tmp/hblink4.sock") |
| `buffer_size` | number | Socket send buffer size (default: 65536) |

### Transport Options

**Unix Socket (`"unix"`)** - Recommended for local dashboard:
- ✅ Fastest performance (~0.5-1μs per event)
- ✅ Same-host only (most secure)
- ✅ Automatic cleanup on startup
- ✅ File permissions control access
- **Use when**: Dashboard runs on same server as HBlink4
- **Configuration**: Only `unix_socket` path is used (host and port fields ignored)

**TCP (`"tcp"`)** - Required for remote dashboard:
- ✅ Remote dashboard capability
- ✅ Dual-stack IPv4/IPv6 support
- ⚠️ Network exposed (use firewall rules)
- **Use when**: Dashboard runs on different server
- **Configuration**: Uses `host_ipv4`, `host_ipv6`, and `port` (unix_socket field ignored)
- **IPv6 detection**: Automatic based on address format

**TCP Dual-Stack Configuration:**

When using TCP transport with HBlink4 and dashboard on **different machines**, you have the same dual-stack options as the main UDP server:

```json
// Localhost (both on same machine) - NO dual-stack issues
"host_ipv4": "127.0.0.1",
"host_ipv6": "::1",
"port": 8765,

// Remote, dual-stack mode (RECOMMENDED for remote dashboard)
"host_ipv4": "",              // Empty = disable IPv4 listener
"host_ipv6": "::",            // Listen on all IPv6 interfaces
"port": 8765,                 // Single port handles both IPv4 and IPv6

// Remote, separate ports (if dual-stack conflicts)
"host_ipv4": "0.0.0.0",
"host_ipv6": "::",
"port": 8765,                 // Note: May need different ports if bind error

// Remote, IPv4-only (simplest)
"disable_ipv6": true,
"host_ipv4": "0.0.0.0",
"port": 8765,
```

**Note**: HBlink4's event emitter tries IPv6 first, then falls back to IPv4 automatically, so dual-stack configuration on the dashboard side works seamlessly.

### Dashboard Configuration Examples

**Local dashboard (Unix socket - recommended):**
```json
"dashboard": {
    "enabled": true,
    "transport": "unix",
    "unix_socket": "/tmp/hblink4.sock"
}
```

**Local dashboard (TCP - if Unix sockets unavailable):**
```json
"dashboard": {
    "enabled": true,
    "transport": "tcp",
    "host_ipv4": "127.0.0.1",
    "host_ipv6": "::1",
    "port": 8765
}
```

**Remote dashboard (TCP):**
```json
"dashboard": {
    "enabled": true,
    "transport": "tcp",
    "host_ipv4": "192.168.1.100",  // Dashboard server IP
    "host_ipv6": "2001:db8::100",  // Dashboard server IPv6 (optional)
    "port": 8765
}
```

**Disable IPv6 for dashboard only:**
```json
"dashboard": {
    "enabled": true,
    "disable_ipv6": true,  // Use IPv4 only
    "transport": "tcp",
    "host_ipv4": "127.0.0.1",
    "port": 8765
}
```

**Important**: Both HBlink4 config (`config/config.json`) and dashboard config (`dashboard/config.json`) must use the **same transport type and connection details**. See [Dashboard Documentation](../dashboard/README.md) for dashboard-side configuration.

## Connection Type Detection

The `connection_type_detection` section configures how connected devices are categorized and displayed in the dashboard. Connections are grouped into four categories with distinct icons:

- 📶 **Repeaters** - Full duplex repeaters and club sites
- 📱 **Hotspots** - Personal hotspots (Pi-Star, WPSD, MMDVM_HS boards)
- 🔗 **Network Inbound** - Servers connecting to us (HBlink, FreeDMR, BrandMeister)
- ❓ **Other** - Unrecognized connection types

```json
{
    "connection_type_detection": {
        "description": "Categorize connections for dashboard display...",
        "hotspot_packages": [
            "mmdvm_hs", "dvmega", "zumspot", "jumbospot", "nanodv",
            "openspot", "dmo", "simplex"
        ],
        "network_packages": [
            "hblink", "freedmr", "brandmeister", "xlx", "dmr+", "tgif", "ipsc"
        ],
        "repeater_packages": [
            "repeater", "duplex", "stm32", "unknown"
        ],
        "hotspot_software": [
            "pi-star", "pistar", "ps4", "wpsd"
        ],
        "network_software": [
            "hblink", "freedmr", "brandmeister", "xlx"
        ]
    }
}
```

| Setting | Type | Description |
|---------|------|-------------|
| `hotspot_packages` | array | Package ID substrings that identify hotspots |
| `network_packages` | array | Package ID substrings that identify network links |
| `repeater_packages` | array | Package ID substrings that identify repeaters |
| `hotspot_software` | array | Software ID substrings that identify hotspots (fallback) |
| `network_software` | array | Software ID substrings that identify network links (fallback) |

### Detection Logic

1. **Primary**: Match against `package_id` field from the RPTC config packet
2. **Fallback**: If no package_id match, check `software_id` field
3. **Default**: If neither matches, connection appears in "Other" section

**Matching behavior:**
- All matching is **case-insensitive** (`MMDVM_HS` matches `mmdvm_hs`)
- **Substring matching** is used (`mmdvm_hs` matches `MMDVM_MMDVM_HS_Dual_Hat`)
- Network patterns are checked first, then hotspots, then repeaters
- Generic `MMDVM` (exact match) defaults to repeater

### Common Package IDs

From real-world connections:

| Package ID | Typical Detection |
|------------|------------------|
| `MMDVM_MMDVM_HS_Hat` | Hotspot (matches `mmdvm_hs`) |
| `MMDVM_MMDVM_HS_Dual_Hat` | Hotspot (matches `mmdvm_hs`) |
| `MMDVM_DMO` | Hotspot (matches `dmo`) |
| `MMDVM_HBlink` | Network (matches `hblink`) |
| `MMDVM` | Repeater (exact match default) |
| `MMDVM_Unknown` | Repeater (matches `unknown`) |

### Customizing Detection

To add custom hardware to a category, add its package_id substring to the appropriate array:

```json
// Example: Add custom hardware to hotspots
"hotspot_packages": [
    "mmdvm_hs", "dvmega", "zumspot", "jumbospot", "nanodv",
    "openspot", "dmo", "simplex",
    "my_custom_hotspot"  // Added
]
```

### Stream Management

The `stream_timeout` and `stream_hang_time` settings control two different aspects of DMR transmission management:

- **`stream_timeout`**: Fallback cleanup timeout (default: 2.0 seconds). This is used **only** when a DMR terminator frame is lost or not received. Under normal operation, streams end immediately when a terminator frame is detected (~60ms). This timeout ensures slot cleanup even if the terminator packet is dropped. **Recommended: 2.0 seconds** to handle worst-case packet loss scenarios.
  
- **`stream_hang_time`**: Slot reservation period (default: 10.0-20.0 seconds). After a stream ends (either via terminator frame or timeout), the timeslot remains reserved for the same RF source for this duration, preventing other stations from hijacking the slot between transmissions in a conversation. **Recommended: 10.0-20.0 seconds** depending on operator speed and network usage patterns.

**How It Works:**
1. DMR transmission begins → stream active
2. DMR terminator frame received → stream ends immediately (~60ms), hang time begins
3. If terminator lost → stream_timeout (2s) triggers cleanup, hang time begins  
4. During hang time → only original source can re-use the slot
5. After hang time expires → slot available to all

**DMR Timing Notes:**
- DMR voice packets transmitted approximately every 60ms
- DMR terminator frame signals end of transmission (primary detection method)
- stream_timeout is a fallback safety mechanism only
- hang_time prevents slot hijacking during multi-transmission conversations

See [Hang Time Documentation](hang_time.md) for detailed explanation of these features.

## Blacklist Rules

The `blacklist` section defines patterns for blocking unwanted repeaters. Each pattern can match by ID, ID range, or callsign.

```json
{
    "blacklist": {
        "patterns": [
            {
                "name": "Pattern Name",
                "description": "Pattern Description",
                "match": {
                    "ids": [123456, 123457]
                    // OR "id_ranges": [[100000, 199999]]
                    // OR "callsigns": ["BADACTOR*"]
                },
                "reason": "Reason for blocking"
            }
        ]
    }
}
```

### Blacklist Pattern Options

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Unique name for the blacklist pattern |
| `description` | string | Detailed description of the pattern |
| `match` | object | One of three match types (see below) |
| `reason` | string | Reason shown when blocking a repeater |

### Match Types

Patterns support three match types (one or more per pattern):
- **Specific IDs**: `"ids"` - Array of DMR IDs
- **ID Ranges**: `"id_ranges"` - Array of [start, end] ranges (inclusive)
- **Callsign Patterns**: `"callsigns"` - Array of patterns with "*" wildcards

Multiple match types in a single pattern are combined with OR logic (any match triggers the rule).

**Examples:**
```json
// Single match type
{
    "name": "Blocked Range",
    "description": "Unauthorized range",
    "match": {
        "id_ranges": [[1000, 1999]]
    },
    "reason": "Unauthorized network"
}

// Multiple ID ranges
{
    "name": "Blocked Multiple Ranges",
    "description": "Multiple unauthorized ranges",
    "match": {
        "id_ranges": [[1000, 1999], [5000, 5999], [9000, 9999]]
    },
    "reason": "Unauthorized network ranges"
}

// Multiple match types (IDs + ranges + callsigns)
{
    "name": "Blocked Combined",
    "description": "Specific IDs, ranges, and callsigns",
    "match": {
        "ids": [123456],
        "id_ranges": [[1000, 1999]],
        "callsigns": ["BADACTOR*"]
    },
    "reason": "Network abuse"
}
```

## Repeater Configurations

The `repeater_configurations` section defines patterns for matching repeaters and their configurations. It includes an optional default configuration and specific patterns.

```json
{
    "repeater_configurations": {
        "patterns": [...],
        "default": {
            "passphrase": "default-key",
            "slot1_talkgroups": [1],
            "slot2_talkgroups": [2]
        }
    }
}
```

**Note:** The `default` configuration is **optional**. If omitted, repeaters that don't match any pattern will be rejected during authentication. This provides better security by requiring explicit configuration for all connecting repeaters.

### Pattern Structure

Each pattern defines a match rule and associated configuration:

```json
{
    "name": "Pattern Name",
    "description": "Optional description for documentation",
    "match": {
        "ids": [312100, 312101],
        "id_ranges": [[312000, 312099]],
        "callsigns": ["WA0EDA*"]
    },
    "config": {
        "passphrase": "secret-key",
        "slot1_talkgroups": [8, 9],
        "slot2_talkgroups": [3100, 3101]
    }
}
```

**Note**: The `description` field is optional and for human documentation only—it is not used by the program.

### Match Types

Repeater patterns support three match types (one or more per pattern):
- **Specific IDs**: `"ids"` - Array of DMR IDs
- **ID Ranges**: `"id_ranges"` - Array of [start, end] ranges (inclusive)
- **Callsign Patterns**: `"callsigns"` - Array of patterns with "*" wildcards

Multiple match types in a single pattern are combined with OR logic (any match triggers the rule).

**Match-All Pattern**: Use `"callsigns": ["*"]` to match any repeater (useful for catch-all patterns).

**Examples:**
```json
// Single ID range
{
    "name": "KS-DMR Range",
    "match": {
        "id_ranges": [[312000, 312099]]
    },
    "config": {
        "passphrase": "ks-dmr-key",
        "slot1_talkgroups": [8, 9],
        "slot2_talkgroups": [3120]
    }
}

// Multiple ID ranges
{
    "name": "Regional Network",
    "description": "Regions 310, 311, and 312",
    "match": {
        "id_ranges": [[310000, 310999], [311000, 311999], [312000, 312999]]
    },
    "config": {
        "passphrase": "regional-key",
        "slot1_talkgroups": [1, 2, 3],
        "slot2_talkgroups": [3100, 3110, 3120]
    }
}

// Multiple match types combined
{
    "name": "KS-DMR Network",
    "description": "All KS-DMR repeaters",
    "match": {
        "ids": [315035, 3129054],
        "id_ranges": [[312001, 312099]],
        "callsigns": ["WA0EDA*"]
    },
    "config": {
        "passphrase": "network-key",
        "slot1_talkgroups": [2, 9],
        "slot2_talkgroups": [3120]
    }
}

// Match-all pattern (catch-all for any repeater not matched above)
{
    "name": "Guest Repeaters",
    "match": {
        "callsigns": ["*"]
    },
    "config": {
        "passphrase": "guest-key",
        "slot1_talkgroups": [8],
        "slot2_talkgroups": [3100]
    }
}
```

### Configuration Options

| Option | Type | Description |
|--------|------|-------------|
| `passphrase` | string | Authentication key for the repeater (required) |
| `slot1_talkgroups` | array | List of allowed talkgroup IDs for timeslot 1 (bidirectional) |
| `slot2_talkgroups` | array | List of allowed talkgroup IDs for timeslot 2 (bidirectional) |
| `trust` | boolean | If true, repeater can use any TG (config TGs become defaults) |
| `default_unit_calls` | boolean | Default unit (private) call participation for repeaters matching this pattern. Repeaters can override via `UNIT=true\|false` in RPTO. (default: `false`) |
| `tx` | boolean | `false` for receive-only peers: they still update last-heard, but unit calls are never routed to them (default: `true`) |
| `roaming` | boolean | A roaming transceiver: a simplex radio HBlink4 retunes per unit call (DMRT/DMRK, see protocol.md) to reach radios on channels in `roaming_channels` that no fixed peer transmits on. Sent group calls only as `roaming_group_calls` allows. Needs `default_unit_calls: true` (default: `false`) |
| `roaming_priority` | integer | Roamers: lower is picked first (default: 100). Equal priorities take turns. A roamer already idling on the call's channel is picked regardless |
| `roaming_interrupt_rx` | string | Roamers: `"never"`, `"last_resort"` or `"as_needed"` — may it drop a call it's hearing to transmit elsewhere (default: `global.roaming_interrupt_rx`). E.g. `"never"` for the one that is the only receiver on its channel |
| `roaming_group_calls` | string | Roamers: `"all"`, `"idle"` or `"none"` — may it carry group calls heard at other sites (default: `global.roaming_group_calls`, itself `"none"`). `"idle"`: only calls on its idle channel, which it reports in its DMRC (protocol.md) |
| `site` | string | Where the peer is, for networks spread over several places on one channel plan (see "Sites" above). Without it, the peer's location and `global.site_radius_km` decide |

**Symmetric Routing:**
The same talkgroup lists control BOTH directions:
- **FROM repeater (inbound)**: Only listed TGIDs are accepted from the repeater
- **TO repeater (outbound)**: Only listed TGIDs are forwarded to the repeater

**Repeater OPTIONS Packet:**
Repeaters can optionally send an OPTIONS packet (RPTO in HomeBrew Protocol) to request specific talkgroups. The server's behavior depends on whether this packet is sent:

| Repeater Behavior | Server Response |
|-------------------|-----------------|
| **No OPTIONS sent** | Uses talkgroups from HBlink4 server configuration (`slot1_talkgroups`, `slot2_talkgroups`) |
| **OPTIONS sent (e.g., "TS1=1,2;TS2=3100")** | Uses intersection of requested TGs and server-configured TGs (cannot expand beyond server config) |
| **OPTIONS sent but empty (no TS specified)** | Uses talkgroups from HBlink4 server configuration |
| **Trusted repeater (trust: true)** | Can request any TGs via OPTIONS; server config becomes defaults if no OPTIONS sent |

**Talkgroup Filtering Modes:**

| Configuration | Behavior | Use Case |
|---------------|----------|----------|
| **Missing/Not configured** | Allow ALL talkgroups | Legacy/unrestricted repeaters |
| **Empty list `[]`** | **DENY ALL** talkgroups | Disable a timeslot completely |
| **List with TGs `[1,2,3]`** | Allow ONLY listed TGs | Normal operation with specific TGs |

⚠️ **IMPORTANT**: An empty list `[]` means "deny all" - no traffic will be accepted or forwarded on that timeslot!

**Examples:**

```json
// Example 1: Allow specific talkgroups
"config": {
    "passphrase": "my-secret-key",
    "slot1_talkgroups": [2, 9],       // Accept/forward ONLY TG 2 and 9 on TS1
    "slot2_talkgroups": [3120, 3121]  // Accept/forward ONLY 3120 and 3121 on TS2
}

// Example 2: Disable a timeslot (deny all)
"config": {
    "passphrase": "my-secret-key",
    "slot1_talkgroups": [],      // DENY ALL traffic on TS1 (slot disabled)
    "slot2_talkgroups": [3120]   // Accept/forward ONLY TG 3120 on TS2
}

// Example 3: No configuration = allow all (backward compatibility)
// If patterns section is omitted or pattern doesn't match,
// the default config applies. If default has no TG lists defined,
// all traffic is allowed (legacy behavior).
"default": {
    "passphrase": "default-password"
    // No slot1_talkgroups or slot2_talkgroups = allow all TGs
}
```

### Trusted Repeaters

The `trust` flag allows designated repeaters to bypass talkgroup restrictions. This is useful for core network repeaters managed by trusted operators.

**Normal Behavior (trust: false or not set):**
- If repeater sends OPTIONS packet: Server uses **intersection** of (requested TGs) ∩ (server-configured TGs)
- If repeater does NOT send OPTIONS: Server uses the TGs from this HBlink4 server configuration
- Repeater limited to only server-configured TGs

**Trusted Behavior (trust: true):**
- If repeater sends OPTIONS packet: Server uses requested TGs **as-is** (no intersection)
- If repeater does NOT send OPTIONS: Server uses the TGs from this HBlink4 server configuration as defaults
- Trusted repeaters can dynamically access any TG without server reconfiguration

**Example:**
```json
{
    "name": "Core Network Repeaters",
    "description": "Trusted repeaters managed by core team",
    "match": {
        "ids": [312000, 312001]
    },
    "config": {
        "passphrase": "core-secret-key",
        "trust": true,
        "slot1_talkgroups": [8, 9],      // Used if repeater doesn't send OPTIONS
        "slot2_talkgroups": [3120, 3121] // Used if repeater doesn't send OPTIONS
    }
}
```

**Use Cases:**
- **Core network repeaters** - Full access for network management
- **Testing/development** - Trusted test repeaters can access any TG
- **Administrative repeaters** - Network monitoring and troubleshooting

**Security Note:** Only assign `trust: true` to repeaters under your direct control. Trusted repeaters have unrestricted access to all talkgroups on the network.

### DMRD Translation (RPTO Extensions)

Trusted repeaters can declare **slot / talkgroup translation** and an optional **outbound rf_src override** via their RPTO (OPTIONS) packet. This lets a repeater's local addressing (what the radio user sees) differ from the network addressing (what the rest of the HBlink4 network sees).

See **[dmrd_translation.md](dmrd_translation.md)** for full semantics, use cases, and operational notes. Quick summary below.

**Extended RPTO grammar:**

```
TS1  = entry[,entry...]    ; subscription (and optional remap) on network TS1
TS2  = entry[,entry...]    ; subscription (and optional remap) on network TS2
SRC  = radio_id            ; outbound rf_src override (group voice only)
UNIT = true|false          ; opt in/out of unit (private) call routing

entry  = net_tgid_spec [ : local_slot [ : local_tgid ] ]
net_tgid_spec = N         ; exact TGID (specificity 3)
              | N-M       ; inclusive range, expanded at parse time (specificity 2)
local_slot = 1 | 2 | *    ; * = preserve the network slot
local_tgid = N | *        ; * = preserve the matched network TGID
```

**`UNIT=`** overrides the pattern's `default_unit_calls` for this repeater. Accepts `true`/`false`/`1`/`0`/`yes`/`no`/`on`/`off` (case-insensitive). **Honored only for trusted repeaters (`trust: true`)** — untrusted repeaters silently stay on the pattern default with a warning logged. If absent from RPTO, the pattern default is used.

Rules:

- **Translation syntax is honored only when `trust: true`**. Non-trusted repeaters keep the legacy TG-subscription behavior; any remap is silently ignored with a warning.
- An entry with **no colon** is a pure subscription (no translation), same as today.
- Ranges (`N-M`) are expanded to individual map entries at parse time. Max 10,000 tgids per range.
- **Wildcards are not supported on the network side** (no `*`, no `N*` prefix). Use a specific TGID or a range.
- **Most-specific wins on collision**: exact (specificity 3) beats range (specificity 2). If two rules claim the same local or network key, the less-specific one is dropped with a warning.
- **`SRC=` applies to group voice only**. Every outgoing packet from this repeater has its rf_src rewritten to this ID — one-way, no reverse translation needed (group destinations have no return address). Use when you want the rest of the network to see all traffic from a repeater as a single "site radio".

**Processing order (important):** translation is the **first** thing that runs on ingress and the **last** thing that runs on egress. ACL checks, hang time, contention detection, and routing all operate in **network vocabulary** — the translation layer normalizes the packet before those checks, and rewrites it back to the target's local vocabulary only as the final step before sending. When you configure `slot1_talkgroups` / `slot2_talkgroups` (or any rule that references a TS/TGID), think in network vocabulary. See [dmrd_translation.md § Processing order](dmrd_translation.md#processing-order) for the full step list.

**Quick examples:**

```
Options = "TS1=9"                          ; legacy: subscribe to net TS1/TG9
Options = "TS1=9:2:9"                      ; net TS1/TG9 ↔ local TS2/TG9 (slot swap)
Options = "TS1=9:2:32"                     ; net TS1/TG9 ↔ local TS2/TG32
Options = "TS1=3000-3200:2:*"              ; range on TS1 delivered on local TS2, tgid preserved
Options = "TS1=3000-3200:2:*,3120:1:3120"  ; same range EXCEPT TG3120 stays on local TS1
Options = "TS1=9:2:32;SRC=9990001"         ; translation + outbound rf_src override
```

When an RPTO with translation arrives during an active stream, the new rules are applied immediately (a warning is logged); the in-flight stream will finish on its old rules within a few seconds.

### Pattern Matching Priority

Patterns are evaluated in the order they appear in the configuration file. The first pattern that matches is used. Within each pattern, all match types (IDs, ID ranges, callsigns) are checked with OR logic.

## Network Outbound

The `outbound_connections` section is **optional** and defines server-to-server links. This allows your HBlink4 server to connect to other HomeBrew Protocol servers as a client (similar to how repeaters connect to your server).

```json
{
    "outbound_connections": [
        {
            "enabled": true,
            "name": "Regional-Master-Server",
            "address": "master.example.com",
            "port": 62031,
            "password": "remote-server-password",
            "radio_id": 312999,
            "callsign": "K0USY-L",
            "rx_frequency": 449000000,
            "tx_frequency": 444000000,
            "power": 50,
            "colorcode": 1,
            "latitude": 38.0,
            "longitude": -97.0,
            "height": 100,
            "location": "Network Link",
            "description": "Link to regional master",
            "url": "https://hblink.example.com",
            "software_id": "HBlink4",
            "package_id": "HBlink4 v2.0",
            "options": "TS1=1,2,3,8,9;TS2=3100,3120,3121,9998"
        }
    ]
}
```

### Required Fields

| Field | Type | Description |
|-------|------|-------------|
| `enabled` | boolean | Enable/disable this connection without removing it |
| `name` | string | Unique name for this connection (used in logs) |
| `address` | string | Hostname or IP address of remote server |
| `port` | number | Port of remote server (typically 62031) |
| `password` | string | Authentication password for remote server |
| `radio_id` | number | DMR ID to use when connecting (must be unique) |

### Optional Metadata Fields

These fields are sent to the remote server during the RPTC (configuration) handshake. If not specified, defaults are used:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `callsign` | string | `""` | Callsign identifier |
| `rx_frequency` | number | `0` | RX frequency in Hz |
| `tx_frequency` | number | `0` | TX frequency in Hz |
| `power` | number | `0` | Power in watts |
| `colorcode` | number | `1` | DMR color code |
| `latitude` | number | `0.0` | Latitude coordinate |
| `longitude` | number | `0.0` | Longitude coordinate |
| `height` | number | `0` | Height in meters |
| `location` | string | `""` | Location description |
| `description` | string | `""` | Connection description |
| `url` | string | `""` | Website URL |
| `software_id` | string | `"HBlink4"` | Software identifier |
| `package_id` | string | `"HBlink4 v2.0"` | Package version |
| `unit_calls_enabled` | boolean | `false` | Whether unit (private) calls traverse this outbound link. When `true`, local unit calls fan out over this link and unit calls arriving on it are forwarded to local repeaters. When `false`, unit calls are dropped at the link boundary. Set to `true` only for peers that participate in unit-call routing (e.g. other HBlink4 servers). |
| `transport` | string | `"udp"` | Transport protocol: `"udp"` (default) or `"sctp"`. SCTP requires Linux kernel support on both ends. The remote server must also be listening on SCTP. |

### Unit (Private) Call Forwarding

When `unit_calls_enabled: true`, this outbound participates in unit-call routing:

- **Local → outbound**: if the target subscriber isn't in our user cache (or its cache entry points to a different location), a local unit call is broadcast to every unit-enabled outbound and local repeater. Once we've heard the target (over this link or another), subsequent calls route one-to-one to wherever the cache says it was last seen.
- **Outbound → local**: unit calls arriving over the link are forwarded to local repeaters using the same cache/broadcast logic, and the source subscriber is cached as reachable via this outbound. A subsequent local call to that subscriber will route back over the same link — an implicit reverse-path forwarding tree when both peers run HBlink4.
- **Anti-loop**: unit calls arriving on an outbound are never re-forwarded to any outbound (including a different one). Each peer owns fanout on its own side of the link.

> ℹ️ **No cross-outbound forwarding today (either call type).** Group calls behave the same way — traffic arriving on an outbound link is delivered to local repeaters only, never to another outbound. For star/tree topologies (the common case) this is correct and loop-safe. For chain or mesh topologies involving three or more HBlink4 servers, traffic will not transit through a middle peer. A future enhancement using stream-ID-based loop detection is tracked in [TODO.md](TODO.md).

### Talkgroup Filtering (OPTIONS)

The `options` field controls which talkgroups are accepted/forwarded on this outbound connection. It uses the same format as the HomeBrew Protocol RPTO (options) packet:

**Format:** `"TS1=tg1,tg2,tg3;TS2=tg4,tg5,tg6"`

**Special values:**
- `*` - Accept all talkgroups on this timeslot
- Empty (no TGs) - Deny all on this timeslot
- Omit options field entirely - Accept all on both timeslots

**Examples:**

```json
// Specific talkgroups on each slot
"options": "TS1=1,2,3,8,9;TS2=3100,3120,3121,9998"

// All talkgroups on both slots
"options": "TS1=*;TS2=*"

// Only TS2 active, TS1 disabled
"options": "TS1=;TS2=3100,3120"

// Only TS1 active, TS2 disabled
"options": "TS1=1,2,3;TS2="

// No options field = accept all (backward compatibility)
// (omit the "options" field entirely)
```

### Bidirectional Traffic

Outbound connections are **bidirectional**:
- **Outbound (local → remote)**: DMR traffic from your local repeaters matching the TG filters is forwarded to the remote server
- **Inbound (remote → local)**: DMR traffic from the remote server matching the TG filters is forwarded to your local repeaters

The same talkgroup filters apply in both directions.

### Connection Behavior

- **Automatic reconnection**: If connection is lost, HBlink4 automatically attempts to reconnect
- **DNS resolution**: The `address` field supports both hostnames (resolved via DNS) and IP addresses
- **Protocol state machine**: Full HomeBrew Protocol handshake (RPTL → MSTCL → RPTK → RPTACK → RPTC → RPTACK → RPTO → RPTACK)
- **Keepalive**: Regular RPTPING/MSTPONG exchanges maintain the connection
- **TDMA slot tracking**: Each outbound connection tracks slot usage independently (respects TDMA constraints)

### Multiple Connections

You can define multiple outbound connections to link with several servers:

```json
"outbound_connections": [
    {
        "enabled": true,
        "name": "Primary-Server",
        "address": "primary.example.com",
        "port": 62031,
        "password": "password1",
        "radio_id": 312999,
        "options": "TS1=1,2,3;TS2=3100,3120"
    },
    {
        "enabled": true,
        "name": "Secondary-Server",
        "address": "secondary.example.com",
        "port": 62031,
        "password": "password2",
        "radio_id": 312998,
        "options": "TS1=8,9;TS2=3121,3122"
    },
    {
        "enabled": false,
        "name": "Backup-Server",
        "address": "backup.example.com",
        "port": 62031,
        "password": "password3",
        "radio_id": 312997,
        "options": "TS1=*;TS2=*"
    }
]
```

**Important**: Each connection must use a **unique radio_id** to avoid conflicts.

### Disabling Connections

Set `enabled: false` to temporarily disable a connection without removing it from the HBlink4 server configuration:

```json
{
    "enabled": false,
    "name": "Disabled-Connection",
    "address": "example.com",
    "port": 62031,
    "password": "password",
    "radio_id": 312996
}
```

## Example Configuration

See `config/config_sample.json` in the repository for a complete example showing all configuration sections.

````

## Example Configuration

See the `config/hblink.json` file in the repository for a complete example configuration with multiple patterns and talkgroups.
