#!/usr/bin/env python3
"""
Test user cache functionality
"""
import pytest
from time import time, sleep
from hblink4.user_cache import UserCache, UserEntry


def test_user_cache_basic():
    """Test basic add and lookup"""
    cache = UserCache(timeout_seconds=600)
    
    # Add user
    cache.update(
        radio_id=3106000,
        repeater_id=312345,
        callsign="N0MJS",
        slot=2,
        talkgroup=3100
    )
    
    # Lookup user
    entry = cache.lookup(3106000)
    assert entry is not None
    assert entry.radio_id == 3106000
    assert entry.repeater_id == 312345
    assert entry.callsign == "N0MJS"
    assert entry.slot == 2
    assert entry.talkgroup == 3100


def test_user_cache_update():
    """Test updating existing user"""
    cache = UserCache(timeout_seconds=600)
    
    # Add user
    cache.update(3106000, 312345, "N0MJS", 2, 3100)
    
    # Update with new repeater
    cache.update(3106000, 312346, "N0MJS", 1, 3101, talker_alias="Cort")
    
    # Verify update
    entry = cache.lookup(3106000)
    assert entry.repeater_id == 312346
    assert entry.slot == 1
    assert entry.talkgroup == 3101
    assert entry.talker_alias == "Cort"


def test_user_cache_expiration():
    """Test that entries expire"""
    cache = UserCache(timeout_seconds=1)  # 1 second timeout
    
    # Add user
    cache.update(3106000, 312345, "N0MJS", 2, 3100)
    
    # Should find it immediately
    entry = cache.lookup(3106000)
    assert entry is not None
    
    # Wait for expiration
    sleep(1.1)
    
    # Should be expired now
    entry = cache.lookup(3106000)
    assert entry is None


def test_user_cache_cleanup():
    """Test cleanup removes expired entries"""
    cache = UserCache(timeout_seconds=1)
    
    # Add multiple users
    for i in range(10):
        cache.update(3106000 + i, 312345, f"USER{i}", 2, 3100)
    
    # All should be present
    assert len(cache._cache) == 10
    
    # Wait for expiration
    sleep(1.1)
    
    # Cleanup should remove all
    removed = cache.cleanup()
    assert removed == 10
    assert len(cache._cache) == 0


def test_user_cache_last_heard():
    """Test get_last_heard returns sorted list"""
    cache = UserCache(timeout_seconds=600)
    
    # Add users with slight delays
    cache.update(3106001, 312345, "USER1", 2, 3100)
    sleep(0.01)
    cache.update(3106002, 312345, "USER2", 2, 3100)
    sleep(0.01)
    cache.update(3106003, 312345, "USER3", 2, 3100)
    
    # Get last heard
    last_heard = cache.get_last_heard(limit=10)
    
    assert len(last_heard) == 3
    # Should be sorted by most recent first
    assert last_heard[0]['radio_id'] == 3106003
    assert last_heard[1]['radio_id'] == 3106002
    assert last_heard[2]['radio_id'] == 3106001


def test_user_cache_limit():
    """Test that last_heard respects limit"""
    cache = UserCache(timeout_seconds=600)
    
    # Add 100 users
    for i in range(100):
        cache.update(3106000 + i, 312345, f"USER{i}", 2, 3100)
    
    # Request only 20
    last_heard = cache.get_last_heard(limit=20)
    assert len(last_heard) == 20


def test_user_cache_private_call_routing():
    """Test using cache for private call routing"""
    cache = UserCache(timeout_seconds=600)
    
    # User heard on repeater 312345
    cache.update(3106000, 312345, "N0MJS", 2, 3100)
    
    # Later, we want to route a private call to this user
    repeater_id = cache.get_repeater_for_user(3106000)
    assert repeater_id == 312345
    
    # Unknown user should return None
    repeater_id = cache.get_repeater_for_user(9999999)
    assert repeater_id is None


def test_user_cache_stats():
    """Test cache statistics"""
    cache = UserCache(timeout_seconds=1)
    
    # Add users
    for i in range(5):
        cache.update(3106000 + i, 312345, f"USER{i}", 2, 3100)
    
    stats = cache.get_stats()
    assert stats['total_entries'] == 5
    assert stats['valid_entries'] == 5
    assert stats['expired_entries'] == 0
    assert stats['timeout_seconds'] == 1
    
    # Wait for expiration
    sleep(1.1)
    
    # Stats should show expired entries
    stats = cache.get_stats()
    assert stats['total_entries'] == 5
    assert stats['valid_entries'] == 0
    assert stats['expired_entries'] == 5


def test_user_cache_disabled():
    """Test that disabled cache can be checked"""
    cache = None  # Simulating disabled cache
    
    # Code should handle None gracefully
    if cache:
        cache.update(3106000, 312345, "N0MJS", 2, 3100)
    
    # No crash should occur
    assert cache is None


def test_user_cache_clear():
    """Test clearing cache"""
    cache = UserCache(timeout_seconds=600)
    
    # Add users
    for i in range(10):
        cache.update(3106000 + i, 312345, f"USER{i}", 2, 3100)
    
    assert len(cache._cache) == 10
    
    # Clear
    cache.clear()
    
    assert len(cache._cache) == 0


if __name__ == '__main__':
    pytest.main([__file__, '-v'])


F = 461687500


def test_reported_expiry_outlives_the_timeout():
    """An external report's expiry (RadioDesk: 8 h without ARS) keeps a route past the cache timeout."""
    cache = UserCache(timeout_seconds=600)
    now = time()
    cache.update(3106000, 0, "", 2, 0, freq=F, source="voice-ext", heard_at=now - 3600, expires_at=now + 3600)
    entry = cache.lookup(3106000)
    assert entry is not None and entry.expires_at == now + 3600
    assert cache.cleanup() == 0


def test_a_report_extends_a_newer_local_entry_on_the_same_channel_only():
    cache = UserCache(timeout_seconds=600)
    now = time()
    cache.update(3106000, 312345, "", 2, 3100, freq=F)                  # heard here just now
    # RadioDesk's report of an hour-old hearing on this channel: older, so it can only extend.
    assert cache.update(3106000, 0, "", 2, 0, freq=F, heard_at=now - 3600, expires_at=now + 7200)
    assert cache.lookup(3106000).expires_at == now + 7200
    assert cache.lookup(3106000).repeater_id == 312345                  # the newer entry is kept
    assert cache.update(3106000, 0, "", 2, 0, freq=F, heard_at=now - 3600, expires_at=now - 1)
    assert cache.lookup(3106000).expires_at == now + 7200               # older than 30 s: can't shorten
    assert not cache.update(3106000, 0, "", 2, 0, freq=F + 25000, heard_at=now - 10, expires_at=now + 99999)


def test_a_report_of_the_same_hearing_can_end_the_route():
    """RadioDesk counts a radio gone (ARS: missed re-registrations): an expiry in the past ends its route."""
    cache = UserCache(timeout_seconds=600)
    now = time()
    cache.update(3106000, 312345, "", 2, 3100, freq=F)
    assert cache.update(3106000, 0, "", 2, 0, freq=F, heard_at=now - 5, expires_at=now - 1)
    assert cache.lookup(3106000) is None


def test_heard_again_keeps_the_reported_expiry():
    cache = UserCache(timeout_seconds=600)
    now = time()
    cache.update(3106000, 0, "", 2, 0, freq=F, heard_at=now - 60, expires_at=now + 7200)
    cache.update(3106000, 312345, "", 2, 3100, freq=F)                  # a local stream on that channel
    assert cache.lookup(3106000).expires_at == now + 7200
    cache.update(3106000, 312346, "", 2, 3100, freq=F + 25000)          # moved: the plain timeout again
    assert cache.lookup(3106000).expires_at is None
