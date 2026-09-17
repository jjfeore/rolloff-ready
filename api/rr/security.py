"""Small, explicit request protections. Limits are process-local, not a WAF."""

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from collections import deque
from urllib.parse import urlsplit

from .models import Problem


class Tokens:
    def __init__(self, secret, clock=time.time):
        self.secret = secret.encode("utf-8")
        self.clock = clock

    def issue(self):
        value = f"{int(self.clock())}.{secrets.token_urlsafe(18)}"
        signature = hmac.new(self.secret, value.encode(), hashlib.sha256).digest()
        return value + "." + base64.urlsafe_b64encode(signature).decode().rstrip("=")

    def verify(self, token):
        try:
            if not isinstance(token, str) or len(token) > 160:
                raise ValueError()
            timestamp, nonce, signature = token.split(".")
            expected = base64.urlsafe_b64encode(hmac.new(self.secret, f"{timestamp}.{nonce}".encode(), hashlib.sha256).digest()).decode().rstrip("=")
            if not hmac.compare_digest(expected, signature):
                raise ValueError()
            age = self.clock() - int(timestamp)
            if age < -5:
                raise ValueError()
            if age > 7200:
                raise Problem(403, "TOKEN_EXPIRED", "This session expired. Refresh the page to continue; save your summary first.")
            return age
        except (TypeError, ValueError):
            raise Problem(403, "INVALID_TOKEN", "Reload the page before trying again.") from None

    @staticmethod
    def _address_data(address):
        # Stable across JSON's integer/float serialization and insignificant spaces.
        p = address["position"]
        return json.dumps([address["id"].strip(), address["label"].strip(), address["countryCode"],
                           f"{float(p['lat']):.7f}", f"{float(p['lng']):.7f}"], ensure_ascii=False, separators=(",", ":"))

    def sign_address(self, address):
        timestamp = str(int(self.clock()))
        payload = "address:" + timestamp + ":" + self._address_data(address)
        signature = hmac.new(self.secret, payload.encode(), hashlib.sha256).hexdigest()
        return timestamp + "." + signature

    def verify_address(self, address):
        try:
            timestamp, signature = address["verification"].split(".")
            age = self.clock() - int(timestamp)
            payload = "address:" + timestamp + ":" + self._address_data(address)
            expected = hmac.new(self.secret, payload.encode(), hashlib.sha256).hexdigest()
            valid = -5 <= age <= 7200 and hmac.compare_digest(expected, signature)
        except (KeyError, TypeError, ValueError, AttributeError):
            valid = False
        if not valid:
            raise Problem(400, "ADDRESS_NOT_VERIFIED", "Search for and select your US address again before continuing.")


class RateLimiter:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.entries = {}
        self.lock = threading.Lock()

    def take(self, key, limit, window):
        now = self.clock()
        with self.lock:
            if len(self.entries) > 2048:
                self.entries = {k: (w, q) for k, (w, q) in self.entries.items() if q and q[-1] > now - w}
                if key not in self.entries and len(self.entries) > 2048:
                    raise Problem(429, "RATE_LIMITED", "The service is busy. Please try again shortly.")
            _, events = self.entries.setdefault(key, (window, deque()))
            while events and events[0] <= now - window:
                events.popleft()
            if len(events) >= limit:
                raise Problem(429, "RATE_LIMITED", "Too many requests. Please wait a minute and try again.")
            events.append(now)


def check_origin(headers, allowed, production=False):
    origin = headers.get("origin", "")
    if origin:
        try:
            if not isinstance(origin, str):
                raise ValueError()
            parsed = urlsplit(origin)
            normalized = f"{parsed.scheme}://{parsed.netloc}"
        except (ValueError, TypeError):
            raise Problem(403, "ORIGIN_NOT_ALLOWED", "Open Rolloff Ready directly to continue.") from None
        if parsed.path or parsed.query or parsed.fragment or parsed.username or normalized not in allowed:
            raise Problem(403, "ORIGIN_NOT_ALLOWED", "Open Rolloff Ready directly to continue.")
    if headers.get("sec-fetch-site") == "cross-site":
        raise Problem(403, "ORIGIN_NOT_ALLOWED", "Open Rolloff Ready directly to continue.")
    if production and not allowed:
        raise Problem(503, "SERVICE_NOT_CONFIGURED", "The service is being configured. Please try again later.")


class Requests:
    """Bounded in-memory duplicate guard. ACS gets a stable operation ID too."""

    def __init__(self, clock=time.monotonic):
        self.clock, self.lock, self.entries = clock, threading.Lock(), {}

    def start(self, request_id, fingerprint):
        with self.lock:
            now = self.clock()
            self.entries = {k: v for k, v in self.entries.items() if now - v[0] < 7200}
            previous = self.entries.get(request_id)
            if previous:
                if previous[1] != fingerprint:
                    raise Problem(409, "REQUEST_CHANGED", "Your request changed. Submit it with a new reference.")
                if previous[2] is None:
                    raise Problem(409, "REQUEST_PENDING", "This request is already being processed. Keep your reference and try again shortly.")
                return previous[2]
            if len(self.entries) >= 1024:
                raise Problem(429, "RATE_LIMITED", "The service is busy. Please try again later.")
            self.entries[request_id] = (now, fingerprint, None)
            return None

    def finish(self, request_id, fingerprint, response):
        with self.lock:
            self.entries[request_id] = (self.clock(), fingerprint, response)

    def release(self, request_id):
        with self.lock:
            self.entries.pop(request_id, None)
