"""
Utility functions for HBlink4

This module contains standalone utility functions that don't depend on 
application state or class instances. These are pure functions that 
can be used throughout the codebase.
"""
import logging
import logging.handlers
import math
import pathlib
from typing import Optional, Tuple, Union

# Type definitions for reusability
PeerAddress = Union[Tuple[str, int], Tuple[str, int, int, int]]


def safe_decode_bytes(data: bytes) -> str:
    """
    Safely decode bytes to UTF-8 string with error handling.
    Used for repeater metadata fields that may contain invalid UTF-8.
    
    Args:
        data: Bytes to decode
        
    Returns:
        Decoded and stripped string, or empty string if data is empty/None
    """
    if not data:
        return ''
    return data.decode('utf-8', errors='ignore').strip()


def normalize_addr(addr: PeerAddress) -> Tuple[str, int]:
    """
    Normalize address tuple to (ip, port) regardless of IPv4/IPv6 format.
    
    Args:
        addr: Address tuple - IPv4: (ip, port) or IPv6: (ip, port, flowinfo, scopeid)
        
    Returns:
        Normalized (ip, port) tuple
    """
    return (addr[0], addr[1])


def rid_to_int(repeater_id: bytes) -> int:
    """
    Convert repeater ID bytes to int.
    
    Args:
        repeater_id: 4-byte repeater ID
        
    Returns:
        Integer representation of repeater ID
    """
    return int.from_bytes(repeater_id, 'big')


def bytes_to_int(value: bytes) -> int:
    """
    Simple bytes to int conversion for logging and display purposes.

    Args:
        value: Bytes to convert

    Returns:
        Integer representation
    """
    return int.from_bytes(value, 'big')


def fmt_ts_tg(net_slot: int, net_tgid, rf_slot: int = None, rf_tgid=None) -> str:
    """
    Format a timeslot/talkgroup pair for log lines.

    Returns "TS/TGID: 2/9" when there's no translation to annotate, or
    "TS/TGID: 2/9 (rf: 1/3172)" when the RF-side (repeater-local) values
    differ from the network-side values. The network side is always the
    primary number because ACLs, subscriptions, and routing all reason in
    that vocabulary.

    Args:
        net_slot: Network-side timeslot (1 or 2)
        net_tgid: Network-side TGID — bytes (3-byte DMR format) or int
        rf_slot: Optional RF-side timeslot (pass None to skip annotation)
        rf_tgid: Optional RF-side TGID — bytes or int

    Returns:
        Formatted string suitable for inclusion in a log line.
    """
    net_tg_int = net_tgid if isinstance(net_tgid, int) else int.from_bytes(net_tgid, 'big')
    base = f"TS/TGID: {net_slot}/{net_tg_int}"
    if rf_slot is None or rf_tgid is None:
        return base
    rf_tg_int = rf_tgid if isinstance(rf_tgid, int) else int.from_bytes(rf_tgid, 'big')
    if rf_slot == net_slot and rf_tg_int == net_tg_int:
        return base
    return f"{base} (rf: {rf_slot}/{rf_tg_int})"


# Default connection type detection patterns (used if not in config)
# These can be overridden in config.json under "connection_type_detection"
DEFAULT_HOTSPOT_PACKAGES = [
    'mmdvm_hs', 'dvmega', 'zumspot', 'jumbospot', 'nanodv',
    'openspot', 'dmo', 'simplex'
]

DEFAULT_NETWORK_PACKAGES = [
    'hblink', 'freedmr', 'brandmeister', 'xlx', 'dmr+', 'tgif', 'ipsc'
]

DEFAULT_REPEATER_PACKAGES = [
    'repeater', 'duplex', 'stm32', 'unknown'
]

DEFAULT_HOTSPOT_SOFTWARE = [
    'pi-star', 'pistar', 'ps4', 'wpsd'
]

DEFAULT_NETWORK_SOFTWARE = [
    'hblink', 'freedmr', 'brandmeister', 'xlx'
]


def detect_connection_type(software_id: bytes, package_id: bytes = None, config: dict = None) -> str:
    """
    Detect connection type based on package_id (primary) or software_id (fallback).
    
    Categories:
    - 'repeater': Full repeaters, club sites
    - 'hotspot': Personal hotspots (Pi-Star, WPSD, MMDVM_HS boards, simplex/DMO)
    - 'network': Network inbound connections (HBlink, FreeDMR, BrandMeister)
    - 'unknown': Unrecognized - defaults shown in "Other" section
    
    Args:
        software_id: Raw bytes from RPTC packet (40 bytes, null-padded)
        package_id: Raw bytes from RPTC packet (40 bytes, null-padded) - primary detection
        config: Optional config dict with connection_type_detection settings
        
    Returns:
        Connection type string: 'repeater', 'hotspot', 'network', or 'unknown'
    """
    # Load patterns from config or use defaults
    detection_config = (config or {}).get('connection_type_detection', {})
    
    hotspot_packages = detection_config.get('hotspot_packages', DEFAULT_HOTSPOT_PACKAGES)
    network_packages = detection_config.get('network_packages', DEFAULT_NETWORK_PACKAGES)
    repeater_packages = detection_config.get('repeater_packages', DEFAULT_REPEATER_PACKAGES)
    hotspot_software = detection_config.get('hotspot_software', DEFAULT_HOTSPOT_SOFTWARE)
    network_software = detection_config.get('network_software', DEFAULT_NETWORK_SOFTWARE)
    
    # Try package_id first (more reliable)
    if package_id:
        pkg_str = package_id.decode('utf-8', errors='ignore').strip().lower()
        
        if pkg_str:
            # Check network first (server connections)
            for network_pkg in network_packages:
                if network_pkg in pkg_str:
                    return 'network'
            
            # Check hotspot patterns
            for hotspot_pkg in hotspot_packages:
                if hotspot_pkg in pkg_str:
                    return 'hotspot'
            
            # Check repeater patterns
            for repeater_pkg in repeater_packages:
                if repeater_pkg in pkg_str:
                    return 'repeater'
            
            # Generic "MMDVM" without qualifiers - likely a repeater
            if pkg_str == 'mmdvm':
                return 'repeater'
    
    # Fallback to software_id if package_id didn't match
    if software_id:
        sw_str = software_id.decode('utf-8', errors='ignore').strip().lower()
        
        if sw_str:
            # Known network software
            if any(x in sw_str for x in network_software):
                return 'network'
            
            # Pi-Star/WPSD variants are typically hotspots
            if any(x in sw_str for x in hotspot_software):
                return 'hotspot'
    
    return 'unknown'


def cleanup_old_logs(log_dir: pathlib.Path, max_days: int, logger: logging.Logger = None) -> None:
    """
    Clean up log files older than max_days based on their date suffix.
    
    Args:
        log_dir: Directory containing log files
        max_days: Maximum age of logs to keep
        logger: Logger instance for output (optional)
    """
    from datetime import datetime, timedelta
    
    current_date = datetime.now()
    cutoff_date = current_date - timedelta(days=max_days)
    
    try:
        for log_file in log_dir.glob('hblink.log.*'):
            try:
                # Extract date from filename (expecting format: hblink.log.YYYY-MM-DD)
                date_str = log_file.name.split('.')[-1]
                file_date = datetime.strptime(date_str, '%Y-%m-%d')
                
                if file_date < cutoff_date:
                    log_file.unlink()
                    if logger:
                        logger.debug(f'Deleted old log file from {date_str}: {log_file}')
            except (OSError, ValueError) as e:
                if logger:
                    logger.warning(f'Error processing old log file {log_file}: {e}')
    except Exception as e:
        if logger:
            logger.error(f'Error during log cleanup: {e}')


def setup_logging(config: dict, logger_name: str = __name__) -> logging.Logger:
    """
    Configure logging with file and console handlers.
    
    Args:
        config: Logging configuration dictionary
        logger_name: Name for the logger
        
    Returns:
        Configured logger instance
    """
    logging_config = config.get('global', {}).get('logging', {})
    
    # Get logging configuration with defaults
    log_file = logging_config.get('file', 'logs/hblink.log')
    file_level = getattr(logging, logging_config.get('file_level', 'DEBUG'))
    console_level = getattr(logging, logging_config.get('console_level', 'INFO'))
    max_days = logging_config.get('retention_days', 30)
    
    log_format = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    
    # Create log directory if it doesn't exist
    log_path = pathlib.Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    
    # Get logger instance
    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.DEBUG)  # Set to lowest level, handlers will filter
    
    # Clean up old log files
    cleanup_old_logs(log_path.parent, max_days, logger)
    
    # Configure rotating file handler with date-based suffix
    file_handler = logging.handlers.TimedRotatingFileHandler(
        str(log_path),
        when='midnight',
        interval=1,
        backupCount=max_days
    )
    # Set the suffix for rotated files to YYYY-MM-DD
    file_handler.suffix = '%Y-%m-%d'
    # Don't include seconds in date suffix
    file_handler.namer = lambda name: name.replace('.%Y-%m-%d%H%M%S', '.%Y-%m-%d')
    
    file_handler.setLevel(file_level)
    file_handler.setFormatter(log_format)
    
    # Configure console handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(console_level)
    console_handler.setFormatter(log_format)
    
    # Add handlers if not already present
    if not logger.handlers:
        logger.addHandler(file_handler)
        logger.addHandler(console_handler)
    
    return logger

# Two frequencies within this many Hz are the same channel (DMR channels are
# 12.5 kHz apart; external reports may give MHz rounded to a few decimals).
FREQ_MATCH_TOLERANCE_HZ = 1250


def parse_freq_hz(value) -> Optional[int]:
    """A frequency as HBP config packets write it (ASCII Hz, e.g. b'461687500'),
    or in MHz (e.g. '461.68750' or 461.6875) → Hz, or None."""
    if isinstance(value, (bytes, bytearray)):
        value = value.decode('ascii', 'ignore')
    try:
        f = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    if f <= 0:
        return None
    return int(round(f * 1e6)) if f < 10_000 else int(round(f))


def parse_colorcode(value) -> Optional[int]:
    """Color code from a config packet / record → 0..15, or None."""
    if isinstance(value, (bytes, bytearray)):
        value = value.decode('ascii', 'ignore')
    try:
        cc = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return cc if 0 <= cc <= 15 else None


def freqs_match(a: Optional[int], b: Optional[int]) -> bool:
    return a is not None and b is not None and abs(a - b) <= FREQ_MATCH_TOLERANCE_HZ


def _ascii_float(value) -> Optional[float]:
    if isinstance(value, (bytes, bytearray)):
        value = value.decode('ascii', 'ignore')
    try:
        return float(str(value).strip().strip('\x00'))
    except (TypeError, ValueError):
        return None


def parse_location(latitude, longitude, height=b'') -> Tuple[Optional[float], Optional[float], Optional[int]]:
    """A peer's location as RPTC (or DMRC's optional tail) writes it — ASCII
    latitude, longitude (degrees) and antenna height above ground (m) — →
    (lat, lon, height). (None, None, None) when unset: missing, unparseable,
    out of range, or 0/0 (what most hotspots send when not configured)."""
    lat, lon = _ascii_float(latitude), _ascii_float(longitude)
    if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
        return None, None, None
    h = _ascii_float(height)
    return lat, lon, (int(round(h)) if h is not None else None)


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two points (degrees), in km."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0 * math.asin(min(1.0, math.sqrt(a)))


def same_site(a_site: Optional[str], a_lat: Optional[float], a_lon: Optional[float],
              b_site: Optional[str], b_lat: Optional[float], b_lon: Optional[float],
              radius_km: float) -> bool:
    """Are two places (a peer, where a radio was heard) at the same site? Both
    named: the names match. Otherwise both located: within `radius_km`.
    Otherwise unknown, which counts as the same site, so networks in one place
    need neither."""
    if a_site and b_site:
        return a_site == b_site
    if None not in (a_lat, a_lon, b_lat, b_lon):
        return distance_km(a_lat, a_lon, b_lat, b_lon) <= radius_km
    return True
