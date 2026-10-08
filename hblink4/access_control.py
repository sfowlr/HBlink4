"""
Access control and configuration matching module for HBlink4
"""

import re
from typing import Optional, Dict, Any, List, Tuple, Union, Literal
from dataclasses import dataclass, field

MatchType = Literal['specific_id', 'id_range', 'callsign']
PatternValue = Union[List[int], List[Tuple[int, int]], List[str]]

class InvalidPatternError(Exception):
    """Raised when a pattern configuration is invalid"""

def validate_pattern(match_type: Literal['specific_id', 'id_range', 'callsign'], pattern: Any) -> None:
    """Validate a pattern value against its declared type"""
    if not isinstance(pattern, list):
        raise InvalidPatternError(f"{match_type} pattern must be a list")

    if match_type == 'specific_id':
        if not all(isinstance(x, int) for x in pattern):
            raise InvalidPatternError("Specific ID patterns must contain only integers")
            
    elif match_type == 'id_range':
        for start, end in pattern:
            if not isinstance(start, int) or not isinstance(end, int):
                raise InvalidPatternError("Range bounds must be integers")
            if start > end:
                raise InvalidPatternError(f"Invalid range: start ({start}) > end ({end})")
            
    else:  # callsign
        if not all(isinstance(p, str) and re.match(r'^[A-Za-z0-9*]+$', p) for p in pattern):
            raise InvalidPatternError("Callsign patterns must contain only alphanumeric characters and *")

class BlacklistError(Exception):
    """Raised when a repeater matches a blacklist pattern"""
    def __init__(self, pattern_name: str, reason: str):
        self.pattern_name = pattern_name
        self.reason = reason
        super().__init__(f"Repeater blocked by {pattern_name}: {reason}")

@dataclass
class BlacklistMatch:
    """Represents a blacklist pattern"""
    name: str
    description: str
    ids: List[int] = field(default_factory=list)
    id_ranges: List[Tuple[int, int]] = field(default_factory=list)
    callsigns: List[str] = field(default_factory=list)
    reason: str = ''

    def __post_init__(self):
        """Validate all patterns"""
        if self.ids:
            validate_pattern('specific_id', self.ids)
        if self.id_ranges:
            validate_pattern('id_range', self.id_ranges)
        if self.callsigns:
            validate_pattern('callsign', self.callsigns)

@dataclass
class RepeaterConfig:
    """Configuration settings for a matched repeater"""
    passphrase: str
    # None = allow all TGs, [] = deny all TGs, [1,2,3] = specific TGs
    slot1_talkgroups: Optional[List[int]] = None
    slot2_talkgroups: Optional[List[int]] = None
    trust: bool = False  # If True, use requested TGs as-is (config = defaults only)
    # Per-pattern default for unit (private) call participation. Repeaters can
    # override via UNIT=true|false in RPTO. Absent UNIT in RPTO = use this.
    default_unit_calls: bool = False
    # False for receive-only peers: they feed last-heard, but unit calls are
    # never routed to them.
    tx: bool = True
    # A roaming transceiver: one simplex radio HBlink4 retunes per call (DMRT)
    # to reach radios on channels no fixed peer transmits on. Used for a unit
    # call only when no fixed TX peer is on the channel; for group calls heard
    # at other sites only as `roaming_group_calls` allows.
    roaming: bool = False
    # Roaming transceivers only. Picking one for a call: one already idling on
    # the channel first; then lower `roaming_priority`, equal ones taking turns
    # (least recently used). `roaming_interrupt_rx`: may it be pulled off a call
    # it's hearing to transmit elsewhere — "never", "last_resort" (only when no
    # other roamer is free) or "as_needed"; None = global.roaming_interrupt_rx.
    roaming_priority: int = 100
    roaming_interrupt_rx: Optional[str] = None
    # Where the peer is, for unit calls on networks spread over several places
    # that share a channel plan: a call goes out only at the site where the
    # target radio was heard. Any name; peers in one place share it. Without
    # it, the peer's location (RPTC / DMRC) and global.site_radius_km decide.
    site: Optional[str] = None
    # Roaming transceivers only: may it carry group calls to its site, heard at
    # another — "all" (any channel in roaming_channels), "idle" (only its idle
    # channel, which its DMRC reports) or "none"; None = global.roaming_group_calls.
    roaming_group_calls: Optional[str] = None

    def __post_init__(self):
        if self.roaming_interrupt_rx not in (None,) + ROAMING_INTERRUPT_MODES:
            raise ValueError(f"roaming_interrupt_rx must be one of {ROAMING_INTERRUPT_MODES}, "
                             f"not {self.roaming_interrupt_rx!r}")
        if self.roaming_group_calls not in (None,) + ROAMING_GROUP_MODES:
            raise ValueError(f"roaming_group_calls must be one of {ROAMING_GROUP_MODES}, "
                             f"not {self.roaming_group_calls!r}")


ROAMING_INTERRUPT_MODES = ('never', 'last_resort', 'as_needed')
ROAMING_GROUP_MODES = ('all', 'idle', 'none')

@dataclass
class PatternMatch:
    """Represents a pattern matching rule for repeater configuration"""
    name: str
    config: RepeaterConfig
    description: str = ''  # Optional description of the pattern
    # Support multiple match types in one pattern (evaluated with OR logic)
    ids: List[int] = field(default_factory=list)
    id_ranges: List[Tuple[int, int]] = field(default_factory=list)
    callsigns: List[str] = field(default_factory=list)

    def __post_init__(self):
        """Validate all patterns"""
        if self.ids:
            validate_pattern('specific_id', self.ids)
        if self.id_ranges:
            validate_pattern('id_range', self.id_ranges)
        if self.callsigns:
            validate_pattern('callsign', self.callsigns)
        
        # At least one match type must be specified
        if not (self.ids or self.id_ranges or self.callsigns):
            raise InvalidPatternError(f"Pattern '{self.name}' must specify at least one match type")

class RepeaterMatcher:
    """
    Handles repeater identification and configuration matching
    """
    def __init__(self, config: Dict[str, Any]):
        self.blacklist = self._parse_blacklist(config.get('blacklist', {"patterns": []}))
        repeater_config = config.get('repeater_configurations', config.get('repeaters', {}))
        self.patterns = self._parse_patterns(repeater_config.get('patterns', []))
        
        # Make default config optional - only use if explicitly provided
        if 'default' in repeater_config:
            self.default_config = RepeaterConfig(**repeater_config['default'])
        else:
            self.default_config = None

    def _parse_blacklist(self, blacklist_config: Dict[str, Any]) -> List[BlacklistMatch]:
        """Parse blacklist patterns from config - supports multiple match types per pattern"""
        result = []
        for pattern in blacklist_config['patterns']:
            match_dict = pattern['match']
            
            # Extract all match types (can be multiple)
            ids = match_dict.get('ids', [])
            id_ranges = [tuple(r) for r in match_dict.get('id_ranges', [])]
            callsigns = match_dict.get('callsigns', [])
            
            result.append(BlacklistMatch(
                name=pattern['name'],
                description=pattern['description'],
                ids=ids,
                id_ranges=id_ranges,
                callsigns=callsigns,
                reason=pattern['reason']
            ))
        return result

    def _parse_patterns(self, patterns: List[Dict[str, Any]]) -> List[PatternMatch]:
        """Parse pattern configurations from config file - supports multiple match types per pattern"""
        result = []
        for pattern in patterns:
            match_dict = pattern['match']
            config = RepeaterConfig(**pattern['config'])
            
            # Extract all match types (can be multiple)
            ids = match_dict.get('ids', [])
            id_ranges = [tuple(r) for r in match_dict.get('id_ranges', [])]
            callsigns = match_dict.get('callsigns', [])
            
            result.append(PatternMatch(
                name=pattern['name'],
                description=pattern.get('description', ''),
                config=config,
                ids=ids,
                id_ranges=id_ranges,
                callsigns=callsigns
            ))
        
        # No need to sort - patterns are evaluated in order, first match wins
        return result

    def _match_pattern(self, radio_id: int, callsign: Optional[str], pattern: Union[BlacklistMatch, PatternMatch]) -> bool:
        """Match a repeater against a pattern - checks all match types with OR logic"""
        # Check specific IDs
        if pattern.ids and radio_id in pattern.ids:
            return True
        
        # Check ID ranges
        if pattern.id_ranges and any(start <= radio_id <= end for start, end in pattern.id_ranges):
            return True
        
        # Check callsign patterns
        if pattern.callsigns and callsign:
            for p in pattern.callsigns:
                pattern_regex = p.replace('*', '.*') if '*' in p else re.escape(p)
                if re.match(f"^{pattern_regex}$", callsign, re.IGNORECASE):
                    return True
        
        return False

    def _check_blacklist(self, radio_id: int, callsign: Optional[str] = None) -> None:
        """Check if a repeater matches any blacklist patterns"""
        for pattern in self.blacklist:
            if self._match_pattern(radio_id, callsign, pattern):
                raise BlacklistError(pattern.name, pattern.reason)

    def get_repeater_config(self, radio_id: int, callsign: Optional[str] = None) -> Optional[RepeaterConfig]:
        """
        Get the configuration for a connecting repeater based on its ID and/or callsign.
        First checks blacklist, then checks patterns in order (first match wins).
        Within each pattern, match priority is: specific IDs -> ID ranges -> callsign patterns
        
        Patterns can now contain multiple match types (ids, id_ranges, callsigns) for flexibility.
        
        Returns None if no patterns match and no default configuration is defined.
        
        Raises:
            BlacklistError: If the repeater matches any blacklist pattern
        """
        # Check blacklist first
        self._check_blacklist(radio_id, callsign)
        
        # Patterns are already sorted by specificity in _parse_patterns
        for pattern in self.patterns:
            if self._match_pattern(radio_id, callsign, pattern):
                return pattern.config

        # If no patterns match, return default configuration (or None if not defined)
        return self.default_config

    def get_pattern_for_repeater(self, radio_id: int, callsign: Optional[str] = None) -> Optional[PatternMatch]:
        """
        Return the pattern that matched this repeater, or None if using default config.
        Used for dashboard display of which pattern was matched.
        
        Raises:
            BlacklistError: If the repeater matches any blacklist pattern
        """
        # Check blacklist first
        self._check_blacklist(radio_id, callsign)
        
        # Find the matching pattern
        for pattern in self.patterns:
            if self._match_pattern(radio_id, callsign, pattern):
                return pattern
        
        # No pattern matched, using default
        return None
