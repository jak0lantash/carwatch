"""One polite request at a time per website, shared by the DoneDeal pager and the saved-ad checker.

Each site gets at least MIN_GAP_SECONDS (+0-5 s) between requests. If a site says to slow down
(HTTP 429/403 or a security check), it is left alone for 5 minutes, doubling up to an hour.
"""
import os
import random
import threading
import time
from datetime import datetime, timezone

MIN_GAP = float(os.environ.get("MIN_GAP_SECONDS", "20"))
MIN_BACKOFF = float(os.environ.get("MIN_BACKOFF_SECONDS", "300"))

_lock = threading.Lock()
_last = {}       # host -> time of last request
_backoff = {}    # host -> current backoff seconds
_blocked = {}    # host -> unix time until which we leave it alone


class RateLimited(Exception):
    """The site asked us to slow down."""


def blocked_until(host):
    t = _blocked.get(host, 0)
    if t <= time.time():
        return None
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def wait_turn(host):
    """Sleep until it's polite to ask `host` again, then reserve the slot."""
    while True:
        with _lock:
            now = time.time()
            until = max(_last.get(host, 0) + MIN_GAP + random.uniform(0, 5), _blocked.get(host, 0))
            if until <= now:
                _last[host] = now
                return
        time.sleep(min(until - now, 30))


def ok(host):
    with _lock:
        _last[host] = time.time()
        _backoff[host] = 0
        _blocked.pop(host, None)


def slow_down(host):
    with _lock:
        b = min(3600, max(MIN_BACKOFF, _backoff.get(host, 0) * 2))
        _backoff[host] = b
        _last[host] = time.time()
        _blocked[host] = time.time() + b
        return b
