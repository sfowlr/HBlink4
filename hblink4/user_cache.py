#!/usr/bin/env python3
"""
User Routing Cache for HBlink4

Tracks DMR users (radio IDs) and their last heard repeater for:
1. Dashboard "Last Heard" display
2. Private call routing optimization (avoids flooding all repeaters)

The cache is time-limited to prevent unbounded memory growth.
Default timeout: 10 minutes (configurable)

Copyright (C) 2025 Cort Buffington, N0MJS
License: GNU GPLv3
"""

import logging
from time import time
from typing import Dict, Optional, List, Tuple
from dataclasses import dataclass, field

LOGGER = logging.getLogger(__name__)


@dataclass
class UserEntry:
    """Cache entry for a heard user.

    Source location is either a local repeater (non-zero `repeater_id`,
    `outbound_name` None) or an outbound server link (`outbound_name` set,
    `repeater_id` is the remote-side repeater id that originated the stream
    if known, 0 otherwise). Routing consumers should inspect `outbound_name`
    first: if set, forward via that outbound; otherwise forward to the local
    `repeater_id`.
    """
    radio_id: int
    repeater_id: int
    callsign: str
    slot: int
    talkgroup: int
    last_heard: float = field(default_factory=time)
    talker_alias: Optional[str] = None
    outbound_name: Optional[str] = None
    # Where the radio is listening, for routing unit calls to a TX-capable peer
    # on the same channel when it was heard on a receive-only one: the heard-on
    # peer's frequency (Hz) and color code, when known. `source` says what the
    # entry came from: 'voice' / 'data' (heard by a peer here), 'voice-ext' /
    # 'ars-ext' (reported by an external system), or 'static' (pinned).
    freq: Optional[int] = None
    colorcode: Optional[int] = None
    source: str = 'voice'
    # Where the peer that heard it is (its RPTC / DMRC location), when it says:
    # degrees, and antenna height above ground in meters.
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    height: Optional[int] = None
    # The heard-on peer's site (its pattern's `site`), or the site an external
    # report names. Unit calls to the radio go out only at this site.
    site: Optional[str] = None

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization"""
        return {
            'radio_id': self.radio_id,
            'repeater_id': self.repeater_id,
            'callsign': self.callsign,
            'slot': self.slot,
            'talkgroup': self.talkgroup,
            'last_heard': self.last_heard,
            'talker_alias': self.talker_alias,
            'outbound_name': self.outbound_name,
            'freq': self.freq,
            'colorcode': self.colorcode,
            'source': self.source,
            'latitude': self.latitude,
            'longitude': self.longitude,
            'height': self.height,
            'site': self.site,
        }


class UserCache:
    """
    Routing cache for tracking users and their last heard repeater.
    
    This cache serves two purposes:
    1. Provides data for dashboard "Last Heard" display
    2. Enables efficient private call routing without flooding all repeaters
    
    Memory management: Entries expire after configurable timeout (default 10 minutes)
    to prevent unbounded growth.
    """
    
    def __init__(self, timeout_seconds: int = 600):
        """
        Initialize user cache.
        
        Args:
            timeout_seconds: How long to keep entries (default 600 = 10 minutes)
        """
        self._cache: Dict[int, UserEntry] = {}
        self._static: Dict[int, UserEntry] = {}
        self._timeout = timeout_seconds
        LOGGER.info(f'User cache initialized with {timeout_seconds}s timeout')
    
    def update(self, radio_id: int, repeater_id: int, callsign: str,
               slot: int, talkgroup: int, talker_alias: Optional[str] = None,
               outbound_name: Optional[str] = None, freq: Optional[int] = None,
               colorcode: Optional[int] = None, source: str = 'voice',
               heard_at: Optional[float] = None, latitude: Optional[float] = None,
               longitude: Optional[float] = None, height: Optional[int] = None,
               site: Optional[str] = None) -> bool:
        """
        Update cache with user activity.

        Args:
            radio_id: Source radio ID (user)
            repeater_id: Local repeater the user was heard on (0 when heard via outbound)
            callsign: User's callsign
            slot: Timeslot (1 or 2)
            talkgroup: Talkgroup/destination ID
            talker_alias: Optional decoded talker alias
            outbound_name: Outbound connection name when the user was heard
                via an outbound server link rather than a local repeater.
                When set, routing decisions will forward unit calls via that
                outbound instead of to any local repeater.
            freq: Frequency (Hz) of the peer the user was heard on, if known
            colorcode: Color code of that peer, if known
            source: What reported it (see UserEntry.source)
            heard_at: When it was heard, for reports from elsewhere; an entry
                already newer than this is kept. Defaults to now.
            latitude, longitude, height: Where the peer that heard it is, if known
            site: That peer's site, if it has one

        Returns:
            False if the update was ignored as older than the cached entry.
        """
        now = time() if heard_at is None else heard_at
        source_desc = f'outbound "{outbound_name}"' if outbound_name else f'repeater {repeater_id}'

        # Update or create entry
        if radio_id in self._cache:
            entry = self._cache[radio_id]
            if heard_at is not None and entry.last_heard > heard_at:
                return False
            entry.repeater_id = repeater_id
            entry.outbound_name = outbound_name
            entry.callsign = callsign
            entry.slot = slot
            entry.talkgroup = talkgroup
            entry.last_heard = now
            entry.freq = freq
            entry.colorcode = colorcode
            entry.source = source
            entry.latitude, entry.longitude, entry.height = latitude, longitude, height
            entry.site = site
            if talker_alias:
                entry.talker_alias = talker_alias
            LOGGER.debug(f'Updated cache: user {radio_id} ({callsign}) on {source_desc} slot {slot} TG {talkgroup}')
        else:
            self._cache[radio_id] = UserEntry(
                radio_id=radio_id,
                repeater_id=repeater_id,
                callsign=callsign,
                slot=slot,
                talkgroup=talkgroup,
                last_heard=now,
                talker_alias=talker_alias,
                outbound_name=outbound_name,
                freq=freq,
                colorcode=colorcode,
                source=source,
                latitude=latitude,
                longitude=longitude,
                height=height,
                site=site,
            )
            LOGGER.debug(f'Added to cache: user {radio_id} ({callsign}) on {source_desc} slot {slot} TG {talkgroup}')
        return True

    def pin(self, radio_id: int, repeater_id: int) -> None:
        """Always route `radio_id` to local `repeater_id` (e.g. a dispatch
        console or gateway peer that owns that ID). Pinned entries never expire and take
        precedence over heard activity. Slot 0 = use the caller's slot."""
        self._static[radio_id] = UserEntry(radio_id=radio_id, repeater_id=repeater_id, callsign='',
                                           slot=0, talkgroup=0, source='static')
        LOGGER.info(f'Pinned user {radio_id} to repeater {repeater_id}')
    
    def lookup(self, radio_id: int) -> Optional[UserEntry]:
        """
        Look up a user in the cache.
        
        Args:
            radio_id: Radio ID to look up
            
        Returns:
            UserEntry if found and not expired, None otherwise
        """
        pinned = self._static.get(radio_id)
        if pinned is not None:
            pinned.last_heard = time()
            return pinned
        if radio_id not in self._cache:
            return None
        
        entry = self._cache[radio_id]
        
        # Check if expired
        if time() - entry.last_heard > self._timeout:
            del self._cache[radio_id]
            LOGGER.debug(f'Removed expired entry for user {radio_id}')
            return None
        
        return entry
    
    def get_repeater_for_user(self, radio_id: int) -> Optional[int]:
        """
        Get the local repeater ID where a user was last heard.

        Returns None if the user was last heard via an outbound connection —
        callers that want to handle both cases should use `get_source_for_user`
        instead.

        Args:
            radio_id: Target radio ID for private call

        Returns:
            Local repeater ID if user found, not expired, and was heard on a
            local repeater. None otherwise.
        """
        entry = self.lookup(radio_id)
        if entry is None or entry.outbound_name is not None:
            return None
        return entry.repeater_id

    def get_source_for_user(self, radio_id: int):
        """
        Get the source location where a user was last heard.

        Returns one of:
          - ('local', repeater_id: int)  — user last heard on a local repeater
          - ('outbound', name: str)       — user last heard via an outbound link
          - None                          — user not cached or cache expired
        """
        entry = self.lookup(radio_id)
        if entry is None:
            return None
        if entry.outbound_name is not None:
            return ('outbound', entry.outbound_name)
        return ('local', entry.repeater_id)
    
    def cleanup(self) -> int:
        """
        Remove expired entries from cache.
        
        This should be called periodically (e.g., once per minute) to prevent
        unbounded memory growth.
        
        Returns:
            Number of entries removed
        """
        now = time()
        expired = []
        
        for radio_id, entry in self._cache.items():
            if now - entry.last_heard > self._timeout:
                expired.append(radio_id)
        
        for radio_id in expired:
            del self._cache[radio_id]
        
        if expired:
            LOGGER.info(f'Cleaned up {len(expired)} expired user cache entries')
        
        return len(expired)
    
    def get_last_heard(self, limit: int = 50) -> List[dict]:
        """
        Get list of recently heard users, sorted by most recent first.
        
        Args:
            limit: Maximum number of entries to return
            
        Returns:
            List of user entries as dictionaries, sorted by last_heard descending
        """
        # Filter out expired entries
        now = time()
        valid_entries = [
            entry for entry in self._cache.values()
            if now - entry.last_heard <= self._timeout
        ]
        
        # Sort by last heard (most recent first)
        sorted_entries = sorted(valid_entries, key=lambda e: e.last_heard, reverse=True)
        
        # Limit results
        return [entry.to_dict() for entry in sorted_entries[:limit]]
    
    def get_stats(self) -> dict:
        """
        Get cache statistics.
        
        Returns:
            Dictionary with cache statistics
        """
        now = time()
        valid = sum(1 for entry in self._cache.values() 
                   if now - entry.last_heard <= self._timeout)
        
        return {
            'total_entries': len(self._cache),
            'valid_entries': valid,
            'expired_entries': len(self._cache) - valid,
            'timeout_seconds': self._timeout
        }
    
    def clear(self) -> None:
        """Clear all entries from cache."""
        count = len(self._cache)
        self._cache.clear()
        LOGGER.info(f'Cleared {count} entries from user cache')
