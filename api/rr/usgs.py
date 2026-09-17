"""Bounded USGS 3DEP terrain estimates, with one source for every sample.

Reported pixel resolution is not vertical accuracy or delivery-site approval.
No addresses, results, or upstream URLs are logged or retained in a cache.
"""

import datetime
import http.client
import json
import math
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict

from .models import CATALOG, CONTAINERS, placement as validate_placement, point as validate_point


SERVICE_URL = "https://elevation.nationalmap.gov/arcgis/rest/services/3DEPElevation/ImageServer"
EARTH_RADIUS_METERS = 6371008.8
FEET_TO_METERS = 0.3048
LATERAL_OFFSET_METERS = 1.5
DEFAULT_THRESHOLD_PERCENT = 3.0  # Advisory visual cue, not an equipment limit.
MAX_RESOLUTION_METERS = 1.01
MAX_CATALOG_ROWS = 10
MAX_SAMPLE_ATTEMPTS = 3
MAX_RESPONSE_BYTES = 64 * 1024
OPERATION_SECONDS = 10.5
REQUEST_SECONDS = 5.0
NATIVE_CACHE_SECONDS = 6 * 60 * 60
NATIVE_CACHE_SIZE = 128
_NETWORK_SLOTS = threading.BoundedSemaphore(4)
_FIELDS = "OBJECTID,Name,LowPS,AcquisitionDate,Category,Source,VerticalDatum,title"
_SUPPORTED_DATUMS = {"navd88", "navd 88", "north american vertical datum of 1988 (navd 88)", "north american vertical datum of 1988 (navd88)"}
_MESSAGES = {
    "no_coverage": "High-resolution terrain data not available for this site.",
    "incomplete_coverage": "One source could not provide all four terrain samples for this placement.",
    "unsupported_source": "Terrain source resolution or provenance could not be verified.",
    "service_unavailable": "Terrain data is temporarily unavailable. Please try again later.",
}


class _Unavailable(Exception):
    def __init__(self, reason):
        self.reason = reason


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _unavailable(reason):
    return {"status": "unavailable", "source": "USGS 3DEP", "reason": reason, "message": _MESSAGES[reason]}


def _number(value, minimum, maximum):
    # Compare before conversion: an arbitrarily large JSON integer must not
    # overflow math.isfinite/float or escape the provider's unavailable result.
    return type(value) in (int, float) and minimum <= value <= maximum


def _text(value, maximum=200):
    return isinstance(value, str) and 0 < len(value.strip()) <= maximum and not any(ord(c) < 32 or ord(c) == 127 for c in value)


def _date(value):
    if not _number(value, -2208988800000, 4102444800000):
        return None
    return datetime.datetime.fromtimestamp(value / 1000, datetime.timezone.utc).date().isoformat()


def destination(origin, bearing_degrees, distance_meters):
    """Mirror src/geometry.ts's spherical destination, including longitude wrap."""
    heading = math.radians(bearing_degrees % 360)
    angular = distance_meters / EARTH_RADIUS_METERS
    lat1, lng1 = math.radians(origin["lat"]), math.radians(origin["lng"])
    lat2 = math.asin(math.sin(lat1) * math.cos(angular) + math.cos(lat1) * math.sin(angular) * math.cos(heading))
    lng2 = lng1 + math.atan2(math.sin(heading) * math.sin(angular) * math.cos(lat1), math.cos(angular) - math.sin(lat1) * math.sin(lat2))
    return {"lat": math.degrees(lat2), "lng": (math.degrees(lng2) + 540) % 360 - 180}


def sample_positions(placement):
    box = CONTAINERS[placement["containerSize"]]
    length = box["lengthFeet"] * FEET_TO_METERS
    width = box["widthFeet"] * FEET_TO_METERS
    truck = CATALOG["truck"]["lengthFeet"] * FEET_TO_METERS
    bearing = placement["bearingDegrees"]
    midpoint = destination(placement, bearing + 180, truck / 2)
    offset = width / 2 + LATERAL_OFFSET_METERS
    points = {
        "containerEnd": destination(placement, bearing, length / 2),
        "truckFront": destination(placement, bearing + 180, length / 2 + truck),
        "left": destination(midpoint, bearing - 90, offset),
        "right": destination(midpoint, bearing + 90, offset),
    }
    return points, length + truck, width + 2 * LATERAL_OFFSET_METERS


def _axis(rise, baseline, threshold, positive, negative):
    percent = 100 * rise / baseline
    return {
        "percent": percent, "baselineMeters": baseline, "riseMeters": rise,
        "classification": "review" if abs(percent) >= threshold else "low",
        "direction": "level" if round(abs(percent), 1) == 0 else positive if percent > 0 else negative,
    }


class UsgsClient:
    def __init__(self, threshold_percent=DEFAULT_THRESHOLD_PERCENT):
        if not _number(threshold_percent, 0.01, 100):
            raise ValueError("Choose a finite positive advisory threshold.")
        self.threshold_percent = float(threshold_percent)
        self._opener = urllib.request.build_opener(
            _NoRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context())
        )
        self._native_cache = OrderedDict()
        self._native_lock = threading.Lock()

    def coverage(self, point):
        points = {"address": validate_point(point, "Address")}
        result = self._run(points)
        if result["status"] == "available":
            result.pop("samples")
            result["message"] = "High-resolution terrain data is available at the selected address. Placement coverage is checked separately."
        return result

    def grade(self, placement):
        points, along_baseline, across_baseline = sample_positions(validate_placement(placement))
        result = self._run(points)
        if result["status"] != "available":
            return result
        samples = result["samples"]
        result.update(
            message="Estimated bare-earth terrain gradients; an operator must confirm the actual site.",
            thresholdPercent=self.threshold_percent,
            lateralOffsetMeters=LATERAL_OFFSET_METERS,
            estimatedAt=datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
            along=_axis(samples["containerEnd"]["elevationMeters"] - samples["truckFront"]["elevationMeters"],
                        along_baseline, self.threshold_percent, "uphill", "downhill"),
            across=_axis(samples["right"]["elevationMeters"] - samples["left"]["elevationMeters"],
                         across_baseline, self.threshold_percent, "right_up", "left_up"),
        )
        return result

    def _run(self, points):
        # Socket timeouts do not bound DNS or slow trickle reads. A daemon worker
        # gives the caller a wall-clock deadline; a global semaphore bounds even
        # stuck workers to four. No new request starts after its deadline.
        slots = _NETWORK_SLOTS
        if not slots.acquire(blocking=False):
            return _unavailable("service_unavailable")
        deadline = time.monotonic() + OPERATION_SECONDS
        done, output = threading.Event(), {}

        def work():
            try:
                output["result"] = self._resolve(points, deadline)
            except _Unavailable as error:
                output["result"] = _unavailable(error.reason)
            except Exception:
                # Never forward third-party exception text, request URLs, or a
                # thread traceback containing submitted coordinates.
                output["result"] = _unavailable("service_unavailable")
            finally:
                slots.release()
                done.set()

        try:
            threading.Thread(target=work, name="usgs-samples", daemon=True).start()
        except RuntimeError:
            slots.release()
            return _unavailable("service_unavailable")
        if not done.wait(max(0, deadline - time.monotonic())) or time.monotonic() >= deadline:
            return _unavailable("service_unavailable")
        return output["result"]

    def _json(self, operation, params, deadline):
        parts = operation.split("/")
        is_info = len(parts) == 2 and parts[1] == "info" and parts[0].isascii() and parts[0].isdigit() and 0 < int(parts[0]) <= 2 ** 31 - 1
        if operation not in ("query", "getSamples") and not is_info:
            raise _Unavailable("service_unavailable")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _Unavailable("service_unavailable")
        request = urllib.request.Request(
            SERVICE_URL + "/" + operation + "?" + urllib.parse.urlencode({"f": "json", **params}),
            headers={"User-Agent": "RolloffReady/1.0", "Accept": "application/json", "Accept-Encoding": "identity"},
        )
        try:
            with self._opener.open(request, timeout=min(REQUEST_SECONDS, remaining)) as response:
                if response.status != 200:
                    raise _Unavailable("service_unavailable")
                declared = response.headers.get("Content-Length")
                if declared is not None and not 0 <= int(declared) <= MAX_RESPONSE_BYTES:
                    raise _Unavailable("service_unavailable")
                kind = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                if kind not in ("application/json", "text/plain") or response.headers.get("Content-Encoding", "identity").lower() != "identity":
                    raise _Unavailable("service_unavailable")
                body = response.read(MAX_RESPONSE_BYTES + 1)
                if len(body) > MAX_RESPONSE_BYTES or time.monotonic() >= deadline:
                    raise _Unavailable("service_unavailable")
                data = json.loads(body)
        except urllib.error.HTTPError as error:
            error.close()
            raise _Unavailable("service_unavailable") from None
        except (urllib.error.URLError, OSError, ValueError, UnicodeError, RecursionError, http.client.HTTPException):
            raise _Unavailable("service_unavailable") from None
        if not isinstance(data, dict) or "error" in data:
            raise _Unavailable("service_unavailable")
        return data

    def _native_resolution(self, identifier, deadline):
        if type(identifier) is not int or not 0 < identifier <= 2 ** 31 - 1:
            raise _Unavailable("unsupported_source")
        with self._native_lock:
            cached = self._native_cache.get(identifier)
            if cached and cached[0] > time.monotonic():
                self._native_cache.move_to_end(identifier)
                return cached[1]
        info = self._json(f"{identifier}/info", {}, deadline)
        if type(info.get("bandCount")) is not int or info["bandCount"] != 1 or info.get("pixelType") not in ("F32", "F64"):
            raise _Unavailable("unsupported_source")
        sizes = [info.get("pixelSizeX"), info.get("pixelSizeY")]
        if any(not _number(value, 0, MAX_RESOLUTION_METERS) or value <= 0 for value in sizes):
            raise _Unavailable("unsupported_source")
        references = []
        for key in ("origin", "extent"):
            part = info.get(key)
            if part is None:
                continue
            if not isinstance(part, dict) or not isinstance(part.get("spatialReference"), dict):
                raise _Unavailable("unsupported_source")
            sr = part["spatialReference"]
            wkid = sr.get("latestWkid", sr.get("wkid"))
            # These EPSG families are UTM in metres: NAD83, WGS84 north,
            # WGS84 south. Unknown/feet/angular CRSs stay unavailable.
            if type(wkid) is not int or not (26901 <= wkid <= 26923 or 32601 <= wkid <= 32660 or 32701 <= wkid <= 32760):
                raise _Unavailable("unsupported_source")
            if "wkid" in sr and sr["wkid"] != wkid:
                raise _Unavailable("unsupported_source")
            references.append(wkid)
        if not references or len(set(references)) != 1:
            raise _Unavailable("unsupported_source")
        resolution = float(max(sizes))
        with self._native_lock:
            self._native_cache[identifier] = (time.monotonic() + NATIVE_CACHE_SECONDS, resolution)
            self._native_cache.move_to_end(identifier)
            while len(self._native_cache) > NATIVE_CACHE_SIZE:
                self._native_cache.popitem(last=False)
        return resolution

    def _resolve(self, points, deadline):
        geometry = {"points": [[p["lng"], p["lat"]] for p in points.values()], "spatialReference": {"wkid": 4326}}
        data = self._json("query", {
            "where": "Category = 1 AND LowPS > 0 AND LowPS <= 1.01",
            "geometry": json.dumps(geometry, separators=(",", ":")), "geometryType": "esriGeometryMultipoint",
            "inSR": "4326", "spatialRel": "esriSpatialRelIntersects", "outFields": _FIELDS,
            "returnGeometry": "false", "resultRecordCount": MAX_CATALOG_ROWS,
            "orderByFields": "AcquisitionDate DESC,OBJECTID DESC",
        }, deadline)
        features = data.get("features")
        if not isinstance(features, list) or len(features) > MAX_CATALOG_ROWS:
            raise _Unavailable("service_unavailable")
        if not features:
            return _unavailable("no_coverage")
        candidates, seen = [], set()
        for feature in features:
            attrs = feature.get("attributes") if isinstance(feature, dict) else None
            if not isinstance(attrs, dict):
                continue
            identifier = attrs.get("OBJECTID")
            if type(identifier) is not int or not 0 < identifier <= 2 ** 31 - 1 or identifier in seen:
                continue
            if attrs.get("Category") != 1 or type(attrs.get("Category")) is not int:
                continue
            if not _number(attrs.get("LowPS"), 0, MAX_RESOLUTION_METERS) or attrs["LowPS"] <= 0:
                continue
            if not all(_text(attrs.get(key)) for key in ("Name", "Source", "VerticalDatum")):
                continue
            # Only the standardized USGS source and documented NAVD88 metre
            # product are supported. Local/unclear vertical datums need a
            # separately verified unit contract before they can be compared.
            if attrs["Source"].strip() != "USGS" or attrs["VerticalDatum"].strip().casefold() not in _SUPPORTED_DATUMS:
                continue
            if not _text(attrs.get("title"), 256) or not attrs["title"].startswith("USGS 1 Meter "):
                continue
            seen.add(identifier)
            candidates.append(attrs)
        candidates.sort(key=lambda a: (a["AcquisitionDate"] if _date(a.get("AcquisitionDate")) else -math.inf, a["OBJECTID"]), reverse=True)
        missing = False
        for candidate in candidates[:MAX_SAMPLE_ATTEMPTS]:
            try:
                native_resolution = self._native_resolution(candidate["OBJECTID"], deadline)
                data = self._json("getSamples", {
                    "geometry": json.dumps(geometry, separators=(",", ":")), "geometryType": "esriGeometryMultipoint",
                    "mosaicRule": json.dumps({"mosaicMethod": "esriMosaicLockRaster", "lockRasterIds": [candidate["OBJECTID"]]}, separators=(",", ":")),
                    "interpolation": "RSP_NearestNeighbor", "returnFirstValueOnly": "false", "outFields": _FIELDS,
                }, deadline)
                samples, _ = self._samples(data, points, candidate)
            except _Unavailable as error:
                if error.reason == "service_unavailable":
                    raise
                missing = missing or error.reason == "incomplete_coverage"
                continue
            return {
                "status": "available", "source": "USGS 3DEP", "message": "Terrain samples are available.",
                "resolutionMeters": native_resolution, "sourceName": candidate["Name"].strip(),
                "sourceDate": _date(candidate.get("AcquisitionDate")), "samples": samples,
            }
        return _unavailable(("no_coverage" if len(points) == 1 else "incomplete_coverage") if missing else "unsupported_source")

    @staticmethod
    def _samples(data, points, candidate):
        rows = data.get("samples")
        if not isinstance(rows, list):
            raise _Unavailable("service_unavailable")
        if len(rows) != len(points):
            raise _Unavailable("incomplete_coverage")
        found, resolutions = {}, []
        named = list(points.items())
        for row in rows:
            if not isinstance(row, dict):
                raise _Unavailable("incomplete_coverage")
            identifier = row.get("locationId")
            if type(identifier) is not int or not 0 <= identifier < len(named) or identifier in found:
                raise _Unavailable("incomplete_coverage")
            if type(row.get("rasterId")) is not int or row["rasterId"] != candidate["OBJECTID"]:
                raise _Unavailable("unsupported_source")
            attrs = row.get("attributes")
            if not isinstance(attrs, dict) or any(attrs.get(key) != candidate.get(key) for key in _FIELDS.split(",")):
                raise _Unavailable("unsupported_source")
            resolution = row.get("resolution")
            if not _number(resolution, 0, MAX_RESOLUTION_METERS) or resolution <= 0 or not math.isclose(resolution, candidate["LowPS"], rel_tol=1e-6):
                raise _Unavailable("unsupported_source")
            name, expected = named[identifier]
            location = row.get("location")
            if not isinstance(location, dict) or not _number(location.get("x"), -180, 180) or not _number(location.get("y"), -85, 85):
                raise _Unavailable("incomplete_coverage")
            sr = location.get("spatialReference")
            if not isinstance(sr, dict) or sr.get("wkid") != 4326 or sr.get("latestWkid", 4326) != 4326:
                raise _Unavailable("incomplete_coverage")
            # Echoed locations identify requests, not independent raster cells.
            if abs(location["x"] - expected["lng"]) > 1e-7 or abs(location["y"] - expected["lat"]) > 1e-7:
                raise _Unavailable("incomplete_coverage")
            value = row.get("value")
            if type(value) not in (int, float, str) or isinstance(value, str) and len(value) > 40:
                raise _Unavailable("incomplete_coverage")
            try:
                elevation = float(value)
            except (ValueError, OverflowError):
                raise _Unavailable("incomplete_coverage") from None
            # Range covers US land terrain, including below-sea-level sites,
            # while excluding service NoData sentinels and non-finite values.
            if not -500 <= elevation <= 9000:
                raise _Unavailable("incomplete_coverage")
            found[identifier] = (name, {**expected, "elevationMeters": elevation})
            resolutions.append(float(resolution))
        return dict(found[index] for index in range(len(named))), max(resolutions)
