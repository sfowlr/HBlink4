"""
External last-heard feed for unit-call routing.

Some radios are heard by receivers that aren't HBlink4 peers, and some report
where they are via data (e.g. ARS registrations) HBlink4 doesn't forward. Any
system that knows where a radio is listening can tell HBlink4 over MQTT; the
user cache keeps the newest report per radio (a newer local stream wins over
an older report and vice versa), and `_resolve_unit_route` sends unit calls
for that radio to the TX-capable peer on that channel.

Config (`global.external_last_heard`):
    {"host": "localhost", "port": 1883, "username": "...", "password": "...",
     "topic": "hblink4/last_heard", "tls": false,
     "status_topic": "hblink4/unit_call"}      optional: unit-call outcomes (voice and
                                               data, unit_data.py) are published here
                                               (HBProtocol._unit_call_status)

Messages on `{topic}` or `{topic}/...` (retained or not): one JSON object or a
list of them:
    {"radio_id": 3120001,          required
     "freq": 461687500,            Hz, or MHz as a string/number ("461.6875"); required
     "colorcode": 1,               optional
     "slot": 2,                    optional (0 / absent = the caller's slot)
     "at": "2026-01-01T12:00:00Z", when it was heard: ISO 8601 with zone, or Unix
                                   seconds; optional (default: now)
     "source": "ars",              free text for logs/dashboard; optional
     "site": "north",              the receiver's site (pattern `site`); optional
     "latitude": 43.03,            where the receiver is (degrees); optional. Unit
     "longitude": -77.72,          calls go out only at the radio's site: by `site`
                                   when both sides have one, else within
                                   global.site_radius_km of this. Neither: the
                                   place already known for the radio on this
                                   channel is kept
     "expires": "2026-01-01T20:00:00Z"}  when it stops being a route (ISO 8601 or
                                   Unix seconds); optional (default: `at` + the
                                   user_cache timeout). For the channel the radio
                                   is already cached on it applies even if the
                                   cache heard it more recently (UserCache.update),
                                   so a reporter that knows the radio (ARS) can keep
                                   a route longer, or end it (a time in the past)

Needs paho-mqtt (>=2.0), imported only when the feed is enabled. paho's
network thread hands each message to the asyncio loop, so the user cache is
only ever touched from the loop.
"""

import json
import logging
from datetime import datetime
from time import time
from typing import Any, Dict, Optional

try:
    from .utils import parse_freq_hz, parse_colorcode, parse_location, freqs_match
except ImportError:
    from utils import parse_freq_hz, parse_colorcode, parse_location, freqs_match

LOGGER = logging.getLogger(__name__)

DEFAULT_TOPIC = 'hblink4/last_heard'


def _epoch(value: Any) -> Optional[float]:
    if isinstance(value, (int, float)) and value > 0:
        return float(value)
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    return dt.timestamp() if dt.tzinfo else None


class ExternalLastHeard:
    def __init__(self, user_cache, topic: str = DEFAULT_TOPIC, is_echo=None) -> None:
        """*is_echo(radio_id, freq, at)*: True when that report is only our own transmission heard
        (HBProtocol._sent_as_echo): such reports are ignored."""
        self._cache = user_cache
        self.topic = topic.rstrip('/')
        self._is_echo = is_echo

    @property
    def subscriptions(self):
        return [self.topic, f'{self.topic}/#']

    def handle(self, topic: str, payload: bytes) -> None:
        """One message: a report or a list of reports."""
        try:
            data = json.loads(payload)
        except (ValueError, TypeError):
            return
        for report in data if isinstance(data, list) else [data]:
            if isinstance(report, dict):
                self._apply(report)

    def _apply(self, report: Dict[str, Any]) -> None:
        try:
            radio_id = int(report['radio_id'])
        except (KeyError, TypeError, ValueError):
            return
        freq = parse_freq_hz(report.get('freq'))
        if freq is None:
            return
        slot = report.get('slot') if report.get('slot') in (1, 2) else 0
        at = _epoch(report.get('at')) or time()
        if self._is_echo is not None and self._is_echo(radio_id, freq, at):
            LOGGER.debug(f'External last-heard: {radio_id} on {freq} Hz is our own transmission, ignored')
            return
        source = str(report.get('source') or 'external')[:16]
        lat, lon, height = parse_location(report.get('latitude'), report.get('longitude'), report.get('height'))
        site = str(report['site'])[:32] if report.get('site') else None
        if site is None and lat is None:
            # No place given: keep the one we have for the radio on this channel
            # (heard here, on a peer whose pattern names its site), e.g. when the
            # reporter's receiver doesn't know where it is.
            known = self._cache.lookup(radio_id)
            if known is not None and freqs_match(known.freq, freq):
                site, lat, lon, height = known.site, known.latitude, known.longitude, known.height
        if self._cache.update(radio_id=radio_id, repeater_id=0, callsign='', slot=slot, talkgroup=0,
                              freq=freq, colorcode=parse_colorcode(report.get('colorcode')),
                              source=f'{source}-ext', heard_at=at,
                              latitude=lat, longitude=lon, height=height, site=site,
                              expires_at=_epoch(report.get('expires'))):
            LOGGER.debug(f'External last-heard: {radio_id} on {freq} Hz TS{slot} [{source}]')

    def start(self, loop, cfg: Dict[str, Any]):
        """Connect and subscribe; returns the paho client (stop with `client.loop_stop()`)."""
        import paho.mqtt.client as mqtt

        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                             client_id=cfg.get('client_id', 'hblink4-last-heard'))
        if cfg.get('username'):
            client.username_pw_set(cfg['username'], cfg.get('password'))
        if cfg.get('tls'):
            client.tls_set()

        def on_connect(c, _userdata, _flags, reason, _props):
            LOGGER.info(f'External last-heard connected to {cfg.get("host")} ({reason}), topic {self.topic}')
            for t in self.subscriptions:
                c.subscribe(t)

        def on_message(_c, _userdata, msg):
            loop.call_soon_threadsafe(self.handle, msg.topic, msg.payload)

        client.on_connect = on_connect
        client.on_message = on_message
        client.connect_async(cfg.get('host', 'localhost'), int(cfg.get('port', 1883)))
        client.loop_start()
        return client
