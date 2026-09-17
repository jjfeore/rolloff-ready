"""Small, bounded HERE adapter. Upstream URLs and credentials stay server-side."""

import datetime
import json
import math
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .models import Problem, us_coordinate


SUPPLIER_TERMS_URL = "https://legal.here.com/en-gb/terms/general-content-supplier-terms-and-notices"
TIMEOUT_SECONDS = 12
ATTRIBUTION_TTL_SECONDS = 24 * 60 * 60
ATTRIBUTION_RETRY_SECONDS = 60
GEOCODE_MAX_BYTES = 256 * 1024
TILE_MAX_BYTES = 1024 * 1024
COPYRIGHT_MAX_BYTES = 512 * 1024

# These explicit Canadian endings must not be approximated into a US address
# by the provider's country filter. This is deliberately not a global address
# parser: province abbreviations require a comma, and street names are intact.
_CANADIAN_ENDING = re.compile(
    r"(?:\bcanada|"
    r"(?:^|[\s,])[ABCEGHJ-NPRSTVXY]\d[ABCEGHJ-NPRSTV-Z][ -]?\d[ABCEGHJ-NPRSTV-Z]\d|"
    r",\s*(?:AB|BC|MB|NB|NL|NS|NT|NU|ON|PE|QC|SK|YT))$",
    re.IGNORECASE,
)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HereClient:
    def __init__(self, api_key: str):
        self._api_key = api_key.strip() if isinstance(api_key, str) else ""
        self._opener = urllib.request.build_opener(
            _NoRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context())
        )
        self._attribution_labels = None
        self._attribution_expires = 0.0
        self._attribution_lock = threading.Lock()

    def _request(self, host, path, params, maximum):
        # Only the methods below can select these fixed endpoints.
        if host not in ("geocode.search.hereapi.com", "maps.hereapi.com") or not path.startswith("/"):
            raise Problem(500, "HERE_REQUEST_INVALID", "The map request could not be prepared.")
        if not self._api_key:
            raise Problem(503, "HERE_NOT_CONFIGURED", "Maps are temporarily unavailable.")
        query = urllib.parse.urlencode({**params, "apiKey": self._api_key})
        request = urllib.request.Request(
            f"https://{host}{path}?{query}",
            headers={"User-Agent": "RolloffReady/1.0", "Accept": "application/json,image/jpeg"},
        )
        try:
            with self._opener.open(request, timeout=TIMEOUT_SECONDS) as response:
                if response.status != 200:
                    raise Problem(502, "HERE_BAD_RESPONSE", "The map provider returned an unexpected response.")
                declared_length = response.headers.get("Content-Length")
                if declared_length is not None:
                    try:
                        if int(declared_length) > maximum:
                            raise Problem(502, "HERE_RESPONSE_TOO_LARGE", "The map provider response was too large.")
                    except ValueError:
                        raise Problem(502, "HERE_BAD_RESPONSE", "The map provider returned an unexpected response.") from None
                body = response.read(maximum + 1)
                if len(body) > maximum:
                    raise Problem(502, "HERE_RESPONSE_TOO_LARGE", "The map provider response was too large.")
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                return body, content_type
        except urllib.error.HTTPError as error:
            status = error.code
            error.close()
            if status == 429:
                raise Problem(503, "HERE_RATE_LIMITED", "Map data is temporarily busy. Please try again later.") from None
            if status in (401, 403):
                raise Problem(503, "HERE_ACCESS_UNAVAILABLE", "Map data is temporarily unavailable.") from None
            raise Problem(502, "HERE_UNAVAILABLE", "Map data could not be loaded. Please try again.") from None
        except (TimeoutError, socket.timeout):
            raise Problem(504, "HERE_TIMEOUT", "Map data took too long to load. Please try again.") from None
        except urllib.error.URLError as error:
            if isinstance(error.reason, (TimeoutError, socket.timeout)):
                raise Problem(504, "HERE_TIMEOUT", "Map data took too long to load. Please try again.") from None
            raise Problem(502, "HERE_UNAVAILABLE", "Map data could not be loaded. Please try again.") from None
        except OSError:
            raise Problem(502, "HERE_UNAVAILABLE", "Map data could not be loaded. Please try again.") from None

    def _json(self, host, path, params, maximum):
        body, content_type = self._request(host, path, params, maximum)
        if content_type != "application/json":
            raise Problem(502, "HERE_BAD_RESPONSE", "The map provider returned an unexpected response.")
        try:
            data = json.loads(body)
        except (ValueError, UnicodeError, RecursionError):
            raise Problem(502, "HERE_BAD_RESPONSE", "The map provider returned an unreadable response.") from None
        if not isinstance(data, dict):
            raise Problem(502, "HERE_BAD_RESPONSE", "The map provider returned an unexpected response.")
        return data

    def _safe_text(self, value, maximum):
        return (
            isinstance(value, str) and 0 < len(value.strip()) <= maximum
            and not any(ord(c) < 32 or ord(c) == 127 for c in value)
            and (not self._api_key or self._api_key not in value)
        )

    def geocode(self, query):
        if not isinstance(query, str) or not 3 <= len(query.strip()) <= 250 or any(ord(c) < 32 for c in query):
            raise Problem(400, "INVALID_INPUT", "Enter a United States street address using 3–250 characters.")
        if _CANADIAN_ENDING.search(query.rstrip(" ,.")):
            raise Problem(400, "US_ONLY", "Rolloff Ready currently supports United States addresses. Enter a US street address.")
        data = self._json("geocode.search.hereapi.com", "/v1/geocode", {
            "q": query.strip(), "in": "countryCode:USA", "limit": 5,
        }, GEOCODE_MAX_BYTES)
        items = data.get("items")
        if not isinstance(items, list):
            raise Problem(502, "HERE_BAD_RESPONSE", "The map provider returned an unexpected response.")
        results, seen = [], set()
        for item in items[:25]:
            if not isinstance(item, dict) or item.get("resultType") not in ("houseNumber", "street"):
                continue
            address, position = item.get("address"), item.get("position")
            if not isinstance(address, dict) or address.get("countryCode") != "USA" or not isinstance(position, dict):
                continue
            lat, lng = position.get("lat"), position.get("lng")
            if any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in (lat, lng)):
                continue
            if not -85 <= lat <= 85 or not -180 <= lng <= 180 or not us_coordinate(lat, lng):
                continue
            label, identifier = address.get("label", item.get("title")), item.get("id")
            if not self._safe_text(label, 500) or not self._safe_text(identifier, 350) or identifier in seen:
                continue
            seen.add(identifier)
            results.append({"id": identifier, "label": label.strip(), "position": {"lat": lat, "lng": lng}, "countryCode": "USA"})
            if len(results) == 5:
                break
        return results

    def tile(self, z, x, y):
        if any(type(value) is not int for value in (z, x, y)) or not 14 <= z <= 20 or not 0 <= x < 2 ** z or not 0 <= y < 2 ** z:
            raise Problem(400, "INVALID_INPUT", "Choose a supported map tile.")
        lng = (x + 0.5) / 2 ** z * 360 - 180
        lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 0.5) / 2 ** z))))
        if not us_coordinate(lat, lng):
            raise Problem(400, "INVALID_INPUT", "Choose a United States map location.")
        body, content_type = self._request("maps.hereapi.com", f"/v3/base/mc/{z}/{x}/{y}/jpeg", {
            "style": "satellite.day", "size": 256,
        }, TILE_MAX_BYTES)
        if content_type != "image/jpeg" or not body.startswith(b"\xff\xd8\xff"):
            raise Problem(502, "HERE_BAD_RESPONSE", "The satellite image could not be loaded.")
        return body, "image/jpeg"

    def attribution(self):
        # Pure satellite attribution includes all supplier labels applicable to
        # this style, conservatively covering every served US viewport/zoom.
        with self._attribution_lock:
            now = time.monotonic()
            if now >= self._attribution_expires:
                try:
                    data = self._json("maps.hereapi.com", "/v3/copyright", {}, COPYRIGHT_MAX_BYTES)
                    groups = data.get("resources", {}).get("base", {}).get("styles", {}).get("satellite.day")
                    copyrights = data.get("copyrights")
                    if not isinstance(groups, list) or not groups or len(groups) > 20 or not isinstance(copyrights, dict):
                        raise ValueError()
                    labels = set()
                    for group in groups:
                        if not isinstance(group, str) or not isinstance(copyrights.get(group), list):
                            raise ValueError()
                        for provider in copyrights[group]:
                            if not isinstance(provider, dict) or not self._safe_text(provider.get("label"), 150):
                                raise ValueError()
                            labels.add(provider["label"].strip())
                    if not labels or len(labels) > 50:
                        raise ValueError()
                    self._attribution_labels = tuple(sorted(labels))
                    self._attribution_expires = now + ATTRIBUTION_TTL_SECONDS
                except (Problem, ValueError, TypeError, AttributeError):
                    # Maxar was verified from HERE's live satellite response on
                    # 2026-09-16. Prefer a prior successful response if available.
                    # A failed refresh is retried soon; never cache failure 24h.
                    if not self._attribution_labels:
                        self._attribution_labels = ("Maxar",)
                    self._attribution_expires = now + ATTRIBUTION_RETRY_SECONDS
            labels = self._attribution_labels
        year = datetime.datetime.now(datetime.timezone.utc).year
        return f"© {year} HERE, {', '.join(labels)}"

    def grade(self, placement):
        # Do not probe quotas on user interaction or guess the SLOPES encoding.
        return {
            "status": "unavailable",
            "message": "Measured street grade is unavailable. Confirm the drop site's slope with your hauler.",
            "source": "HERE Map Attributes",
        }
