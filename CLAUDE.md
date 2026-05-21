# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## MCP Tools: code-review-graph

**IMPORTANT: This project has a knowledge graph. ALWAYS use the
code-review-graph MCP tools BEFORE using Grep/Glob/Read to explore
the codebase.** The graph is faster, cheaper (fewer tokens), and gives
you structural context (callers, dependents, test coverage) that file
scanning cannot.

### When to use graph tools FIRST

- **Exploring code**: `semantic_search_nodes` or `query_graph` instead of Grep
- **Understanding impact**: `get_impact_radius` instead of manually tracing imports
- **Code review**: `detect_changes` + `get_review_context` instead of reading entire files
- **Finding relationships**: `query_graph` with callers_of/callees_of/imports_of/tests_for
- **Architecture questions**: `get_architecture_overview` + `list_communities`

Fall back to Grep/Glob/Read **only** when the graph doesn't cover what you need.

### Key Tools

| Tool | Use when |
|------|----------|
| `detect_changes` | Reviewing code changes — gives risk-scored analysis |
| `get_review_context` | Need source snippets for review — token-efficient |
| `get_impact_radius` | Understanding blast radius of a change |
| `get_affected_flows` | Finding which execution paths are impacted |
| `query_graph` | Tracing callers, callees, imports, tests, dependencies |
| `semantic_search_nodes` | Finding functions/classes by name or keyword |
| `get_architecture_overview` | Understanding high-level codebase structure |
| `refactor_tool` | Planning renames, finding dead code |

### Workflow

1. The graph auto-updates on file changes (via hooks).
2. Use `detect_changes` for code review.
3. Use `get_affected_flows` to understand impact.
4. Use `query_graph` pattern="tests_for" to check coverage.

## Project Overview

HBlink4 is a **repeater-centric DMR (Digital Mobile Radio) server** implementing the HomeBrew protocol for amateur radio networks. It's a complete ground-up rewrite designed for modern efficiency and low-latency operation.

### Architecture

The project consists of two main components:

1. **Core Server** (`hblink4/` package):
   - Pure asyncio-based UDP server (no external web framework)
   - Handles HomeBrew protocol parsing and DMR stream tracking
   - Per-repeater configuration and access control
   - Event emission system for dashboard communication
   - User cache for private call routing

2. **Web Dashboard** (`dashboard/` package):
   - FastAPI + Uvicorn + WebSockets
   - Real-time monitoring with 1-second updates
   - Last heard users with callsign/alias lookup
   - Active stream tracking with duration counters
   - Connection categorization (repeaters, hotspots, network links)

**Design Philosophy**: The "hot path" (DMR packet processing) must remain ultra-efficient. The dashboard must not consume resources that degrade core server performance. Target deployment: modern Raspberry Pi or equivalent.

## Development Setup

### Environment Preparation

```bash
# Create and activate virtual environment
python3 -m venv venv
source venv/bin/activate

# Install core dependencies
pip install -r requirements.txt

# Install dashboard dependencies (optional, required for full development)
pip install -r requirements-dashboard.txt
```

### Running

```bash
# Start core server only (defaults to config/config.json)
python3 run.py

# Start with explicit config file
python3 run.py /path/to/config.json

# Start dashboard only (separate process)
python3 run_dashboard.py [host] [port]
# Defaults: localhost:8080

# Start both together (development)
./run_all.sh
```

Access dashboard at http://localhost:8080

### Testing

```bash
# Run all tests
python3 -m pytest tests/ -v

# Run specific test file
python3 -m pytest tests/test_access_control.py -v

# Run specific test
python3 -m pytest tests/test_access_control.py::TestRepeaterMatcher::test_specific_id_match -v

# Run with coverage
python3 -m pytest tests/ --cov=hblink4 --cov-report=term-missing
```

### Configuration

**Primary config**: `config/config.json` (server settings, access control, routing)
**Dashboard config**: `dashboard/config.json` (created automatically on first run)

Start with sample files:
```bash
cp config/config_sample.json config/config.json
# Edit config/config.json as needed
```

Key configuration patterns:
- **Transport**: Server and dashboard must use matching transport (unix socket or TCP)
  - Unix socket (`/tmp/hblink4.sock`): local, fastest
  - TCP: remote dashboard capability
- **Repeater matching**: Pattern-based (specific IDs, ID ranges, callsign wildcards with `*`)
- **Access control**: Blacklist patterns checked first, then repeater configurations

## Code Architecture

### Module Structure

| Module | Purpose |
|--------|---------|
| `hblink4/hblink.py` | Main protocol handler; `HBProtocol` class processes all packets (UDP + SCTP) |
| `hblink4/sctp.py` | Optional SCTP transport: availability probe, `SCTPInboundProtocol`, `SCTPOutboundProtocol` |
| `hblink4/models.py` | Data classes: `RepeaterState`, `StreamState`, `OutboundState`, `OutboundConnectionConfig` |
| `hblink4/access_control.py` | Pattern matching (`RepeaterMatcher`) and blacklist enforcement |
| `hblink4/events.py` | Event emission abstraction (Unix socket / TCP transport) |
| `hblink4/user_cache.py` | User routing cache for private calls and dashboard "Last Heard" |
| `hblink4/protocol.py` | Pure functions for DMR packet parsing and terminator detection |
| `hblink4/config.py` | Configuration loading and validation |
| `hblink4/constants.py` | Protocol command constants and DMR sync patterns |
| `hblink4/utils.py` | Pure utility functions (ID formatting, logging, connection type detection) |
| `hblink4/lc.py` | Link Control encoding/decoding for DMR data calls and talker aliases |
| `dashboard/server.py` | FastAPI app with WebSocket event receiver and REST endpoints |
| `dashboard/user_db.py` | RadioID.net user database caching with background refresh |

### Import Pattern

Modules use dual import paths for flexibility (package-relative first, fallback to direct):

```python
try:
    from .constants import RPTA, RPTL
    from .utils import safe_decode_bytes
except ImportError:
    from constants import RPTA, RPTL
    from utils import safe_decode_bytes
```

This allows running modules standalone or as a package.

### Key Patterns

#### DMR Stream Tracking

Streams are tracked in `HBProtocol._streams` with dual termination detection:

1. **Immediate termination** (~60ms): Check for DMR terminator frame (function `is_dmr_terminator()` in `protocol.py`)
2. **Fallback timeout** (~2 seconds): Configured in `global.stream_timeout`

Event emission happens every 10 superframes (1 second) for dashboard updates. The `StreamState` dataclass tracks:
- Active status and timing
- Call type (group/private/data) and packet count
- Routing metadata for outbound forwarding
- Hang time to prevent conversation interruption

#### Access Control Flow

1. `RepeaterMatcher.find_config()` looks up repeater against blacklist and configuration patterns
2. Blacklist patterns raise `BlacklistError` (checked first)
3. Returns `RepeaterConfig` with per-repeater settings
4. Pattern types: `specific_id`, `id_range` (with bounds checking), `callsign` (with `*` wildcards)

#### Event Emission

`EventEmitter` uses non-blocking sockets with transport abstraction:
- **Unix socket**: ~0.5-1μs per event
- **TCP**: ~5-15μs per event, supports remote dashboards

Event types: `stream_start`, `stream_end`, `stream_update`, `repeater_connected`, `repeater_disconnected`, `user_cache_update`

#### User Cache and Private Call Routing

`UserCache` stores last heard location for each radio ID with TTL expiration. Used for:
1. Dashboard "Last Heard" display (10 most recent, expandable to 50)
2. Private call routing optimization (avoid flooding all repeaters)
3. Per-user caching of outbound link source (for hearing users from other servers)

Cache timeout default: 600 seconds (10 minutes). Must be >= 60 seconds; longer timeouts are safer for multi-minute DMR transmissions.

#### Configuration Type Detection

Connections categorized by device type via substring matching on `package_id` (primary) or `software_id` (fallback):
- **Repeaters** (📶): Full duplex sites (generic MMDVM_Unknown, STM32)
- **Hotspots** (📱): Personal devices (MMDVM_HS, Pi-Star, WPSD)
- **Network Inbound** (🔗): Server-to-server links (HBlink, FreeDMR)
- **Other** (❓): Unrecognized

Patterns configured in `config.json` under `connection_type_detection`.

#### SCTP Transport

Optional alternative to UDP, enabled via `sctp_enabled: true` in config. Linux only (macOS falls back gracefully).

- `RepeaterState.send` and `OutboundState.send` hold a `Callable[[bytes], None]` — UDP closes over `(transport, addr)`, SCTP uses `transport.write`
- `SCTPInboundProtocol.data_received` delegates to `HBProtocol.datagram_received` — zero handler duplication
- Hot path uses `repeater.send(data)` directly; pre-auth messages go through `_send_packet` which does a dict lookup in `_sctp_transports`
- `SCTP_NODELAY` always enabled to prevent Nagle buffering of small DMR packets
- Application-level RPTPING/MSTPONG keepalives still used (HomeBrew protocol requires them)

### Hot Path Considerations

The packet processing loop in `HBProtocol.datagram_received()` executes for every DMR packet:

- `protocol.py` functions are pure and optimized for speed (hot path)
- Avoid allocations and I/O in packet handling
- Event emission is buffered (not sent on every packet)
- Dashboard updates aggregate every 1 second
- Unit cache cleanup is periodic (every 60 seconds), not per-packet
- Hot-path sends use `repeater.send(data)` — no dict lookup, no addr normalization

### Outbound Connections

Server-to-server links supporting UDP or SCTP transport:

- Configured in `config.json` → `outbound_connections`
- Each outbound maintains separate connection with its own protocol instance (`OutboundProtocol` for UDP, `SCTPOutboundProtocol` for SCTP)
- `OutboundState` tracks connection health (last ping, missed pings)
- DMRD translation via RPTO (Repeater Protocol Options) allows per-repeater TS/TGID mapping
- Unit call forwarding optional per outbound link (`unit_calls_enabled`)

### Data Call Classification

Data calls are identified by Link Control analysis in `lc.py`:
- Voice calls: "group", "private", "unknown"
- Data calls: "data" (logged differently, not forwarded as voice)
- Talker alias extraction for user identification

## Testing Strategy

Test files in `tests/`:

- **test_access_control.py**: Pattern matching (IDs, ranges, callsigns, blacklists)
- **test_routing_optimization.py**: RPTO translation and TGID filtering
- **test_user_cache.py**: TTL expiration and lookup
- **test_stream_tracking.py**: Stream lifecycle management
- **test_terminator_detection.py**: DMR terminator frame detection
- **test_lc.py**: Link Control encoding/decoding
- **test_hang_time.py**: Slot hang time and conversation continuity
- **test_connection_type.py**: Device type categorization logic
- **test_sctp.py**: SCTP protocol delegation, send callable patterns, availability detection (mock-based tests run everywhere; real socket tests Linux-only)

Load test configurations from `config/config_sample.json` to validate patterns work as documented.

## Deployment

### Systemd Services

Service files for production deployment:

```bash
# Install service files
sudo cp hblink4.service /etc/systemd/system/
sudo cp hblink4-dash.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable hblink4 hblink4-dash
sudo systemctl start hblink4 hblink4-dash

# View logs
sudo journalctl -u hblink4 -f
sudo journalctl -u hblink4-dash -f
```

**Important**: Services run as the user who owns the installation directory. Ensure write access for logs and dashboard data.

### Dual-Stack IPv4/IPv6

HBlink4 natively supports dual-stack operation:

```json
{
  "global": {
    "bind_ipv4": "0.0.0.0",
    "bind_ipv6": "::",
    "port_ipv4": 62031,
    "port_ipv6": 62031,
    "disable_ipv6": false
  }
}
```

If separate ports are needed due to dual-stack binding conflicts:
```json
{
  "port_ipv4": 62031,
  "port_ipv6": 62032
}
```

## Git Workflow

Current version: **v4.8.0** (unit call routing, data call classification)

Key branches:
- `main`: Stable releases
- `feature/*`: Feature development (consult before major changes)

Commit messages follow conventional commits format. Example:
```
feat: implement unit (private) call routing
fix: honor empty TS1=/TS2= in RPTO as explicit deny-all
docs: audit and refresh ahead of merge to main
```

## Documentation

Comprehensive docs in `docs/`:

- **configuration.md**: Complete reference for all settings
- **stream_tracking.md**: Detailed explanation of stream lifecycle
- **stream_tracking_diagrams.md**: Visual walkthrough of state transitions
- **routing.md**: Inbound/outbound filtering and contention
- **dmrd_translation.md**: Per-repeater TS/TGID remap (RPTO)
- **hang_time.md**: Preventing conversation interruption
- **protocol.md**: HomeBrew DMR protocol specification
- **integration.md**: Using HBlink4 as a module

## Important Notes

1. **Config Pattern Matching**: Multiple match types (IDs, ranges, callsigns) in one pattern are evaluated with **OR logic**. Each type must be valid (no mixing in a single match condition).

2. **Stream Timeout**: Default `stream_timeout` is 2.0 seconds as a fallback for lost terminators. Terminator detection typically triggers within 60ms.

3. **User Cache Timeout**: Must be >= 60 seconds. Set much higher (600+ seconds) for normal operation because `last_heard` is only refreshed at stream start (PTT), not per-packet.

4. **Hang Time**: Prevents slot contention when the same user keys up again immediately. Default 10 seconds is safe for most networks.

5. **Transport Sync**: Both `config/config.json` and `dashboard/config.json` must use matching transport settings (both unix socket or both TCP).

6. **No External Framework Dependency**: Core server uses pure asyncio. Dashboard is the only component with external framework dependency (FastAPI/Uvicorn).

7. **SCTP is Linux-only**: macOS has no kernel SCTP support. When `sctp_enabled: true` on macOS, a warning is logged and the server continues UDP-only. Tests use mocks to run on all platforms.

