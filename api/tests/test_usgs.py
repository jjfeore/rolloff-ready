"""USGS provider tests. All requests are mocked; no live service calls."""

import copy
import io
import json
import math
import pathlib
import ssl
import sys
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from unittest.mock import Mock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from rr import usgs
from rr.models import CATALOG, CONTAINERS, Problem, distance_m


POINT = {"lat": 47.60395, "lng": -122.3305}
PLACEMENT = {**POINT, "containerSize": 20, "bearingDegrees": 0}
PRIVATE = "private-upstream-data"


class Response:
    def __init__(self, data, content_type="application/json", headers=None, status=200):
        self.body = data if isinstance(data, bytes) else json.dumps(data).encode()
        self.headers = {"Content-Type": content_type, **(headers or {})}
        self.status = status
        self.read_sizes = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, maximum):
        self.read_sizes.append(maximum)
        return self.body[:maximum]


def attributes(identifier=10, date=1625788800000, **changes):
    return {"OBJECTID": identifier, "Name": "WA_Test_Project", "LowPS": 1, "Category": 1,
            "AcquisitionDate": date, "Source": "USGS",
            "VerticalDatum": "North American Vertical Datum of 1988 (NAVD 88)",
            "title": "USGS 1 Meter 10 x55y528 WA_Test_Project", **changes}


def native(**changes):
    return {"pixelSizeX": 1, "pixelSizeY": 1, "bandCount": 1, "pixelType": "F32",
            "origin": {"spatialReference": {"wkid": 26910, "latestWkid": 26910}},
            "extent": {"spatialReference": {"wkid": 26910, "latestWkid": 26910}}, **changes}


class Provider:
    def __init__(self):
        self.candidates = [attributes()]
        self.info = native()
        self.values = [100.4, 100.0, 100.0, 100.2]
        self.transform = lambda rows, identifier: rows
        self.requests = []

    def open(self, request, timeout):
        parsed = urllib.parse.urlparse(request.full_url)
        params = {key: values[0] for key, values in urllib.parse.parse_qs(parsed.query).items()}
        operation = parsed.path.rsplit("/", 1)[1]
        self.requests.append((operation, params, timeout, request))
        if operation == "query":
            return Response({"features": [{"attributes": a} for a in self.candidates]})
        if operation == "info":
            return Response(self.info)
        if operation != "getSamples":
            raise AssertionError("Unexpected endpoint")
        identifier = json.loads(params["mosaicRule"])["lockRasterIds"][0]
        attrs = next(a for a in self.candidates if a["OBJECTID"] == identifier)
        positions = json.loads(params["geometry"])["points"]
        rows = [{"locationId": index, "rasterId": identifier, "resolution": 1,
                 "location": {"x": point[0], "y": point[1], "spatialReference": {"wkid": 4326, "latestWkid": 4326}},
                 "value": self.values[index], "attributes": copy.deepcopy(attrs)} for index, point in enumerate(positions)]
        return Response({"samples": self.transform(rows, identifier)})


class GeometryTests(unittest.TestCase):
    def test_all_sizes_headings_and_us_latitudes_preserve_physical_baselines(self):
        for lat, lng in ((13.45, 144.75), (21.3, -157.8), (37.7, -122.4), (47.6, -122.3), (65, -150), (-14.3, -170.7)):
            for size, box in CONTAINERS.items():
                for heading in (0, 45, 90, 180, 270, 359.99):
                    with self.subTest(lat=lat, size=size, heading=heading):
                        placement = {"lat": lat, "lng": lng, "containerSize": size, "bearingDegrees": heading}
                        points, along, across = usgs.sample_positions(placement)
                        truck = CATALOG["truck"]["lengthFeet"] * 0.3048
                        length = box["lengthFeet"] * 0.3048
                        self.assertAlmostEqual(along, length + truck, places=10)
                        self.assertAlmostEqual(across, box["widthFeet"] * 0.3048 + 3, places=10)
                        self.assertAlmostEqual(distance_m(points["containerEnd"], points["truckFront"]), along, places=6)
                        self.assertAlmostEqual(distance_m(points["left"], points["right"]), across, places=6)
                        self.assertAlmostEqual(distance_m(placement, points["containerEnd"]), length / 2, places=6)
                        self.assertAlmostEqual(distance_m(placement, points["truckFront"]), length / 2 + truck, places=6)
                        midpoint = usgs.destination(placement, heading + 180, truck / 2)
                        self.assertAlmostEqual(distance_m(midpoint, points["left"]), across / 2, places=6)
                        self.assertAlmostEqual(distance_m(midpoint, points["right"]), across / 2, places=6)

    def test_cardinal_sides_and_antimeridian(self):
        north, _, _ = usgs.sample_positions(PLACEMENT)
        self.assertGreater(north["containerEnd"]["lat"], north["truckFront"]["lat"])
        self.assertGreater(north["right"]["lng"], north["left"]["lng"])
        east, _, _ = usgs.sample_positions({**PLACEMENT, "bearingDegrees": 90})
        self.assertGreater(east["containerEnd"]["lng"], east["truckFront"]["lng"])
        self.assertLess(east["right"]["lat"], east["left"]["lat"])
        origin = {"lat": 65, "lng": 179.99999}
        point = usgs.destination(origin, 90, 30)
        self.assertLess(point["lng"], 0)
        self.assertAlmostEqual(distance_m(origin, point), 30, places=6)


class UsgsTests(unittest.TestCase):
    def setUp(self):
        self.client = usgs.UsgsClient()
        self.provider = Provider()
        self.client._opener = Mock()
        self.client._opener.open.side_effect = self.provider.open

    def assert_unavailable(self, result, reason=None):
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["source"], "USGS 3DEP")
        if reason:
            self.assertEqual(result["reason"], reason)
        for key in ("along", "across", "samples", "resolutionMeters", "estimatedAt"):
            self.assertNotIn(key, result)
        self.assertNotIn(PRIVATE, json.dumps(result))

    def test_available_grade_contract_signs_and_provenance(self):
        result = self.client.grade(PLACEMENT)
        self.assertEqual(result["status"], "available")
        self.assertEqual(result["sourceName"], "WA_Test_Project")
        self.assertEqual(result["sourceDate"], "2021-07-09")
        self.assertEqual(result["resolutionMeters"], 1)
        self.assertEqual(result["thresholdPercent"], 3)
        self.assertEqual(result["lateralOffsetMeters"], 1.5)
        self.assertTrue(result["estimatedAt"].endswith("Z"))
        self.assertAlmostEqual(result["along"]["riseMeters"], 0.4)
        self.assertAlmostEqual(result["along"]["percent"], 40 / ((22 + 43.2) * 0.3048))
        self.assertEqual(result["along"]["direction"], "uphill")
        self.assertEqual(result["along"]["classification"], "low")
        self.assertEqual(result["across"]["direction"], "right_up")
        self.assertEqual(result["across"]["classification"], "review")
        self.assertEqual(set(result["samples"]), {"containerEnd", "truckFront", "left", "right"})

    def test_negative_grades_and_actual_zero_are_available(self):
        self.provider.values = [0, 1, 1, 0]
        result = self.client.grade(PLACEMENT)
        self.assertEqual(result["along"]["direction"], "downhill")
        self.assertEqual(result["across"]["direction"], "left_up")
        self.assertLess(result["along"]["percent"], 0)
        self.assertLess(result["across"]["percent"], 0)
        self.provider.values = [0] * 4
        result = self.client.grade(PLACEMENT)
        for name in ("along", "across"):
            self.assertEqual(result[name]["percent"], 0)
            self.assertEqual(result[name]["direction"], "level")
            self.assertEqual(result[name]["classification"], "low")

    def test_advisory_threshold_and_display_level_rounding(self):
        for rise, classification, direction in ((0.049, "low", "level"), (0.051, "low", "uphill"), (2.999, "low", "uphill"), (3, "review", "uphill"), (-3, "review", "downhill")):
            axis = usgs._axis(rise, 100, 3, "uphill", "downhill")
            self.assertEqual(axis["classification"], classification)
            self.assertEqual(axis["direction"], direction)
        self.client.threshold_percent = 1
        self.assertEqual(self.client.grade(PLACEMENT)["along"]["classification"], "review")
        for value in (None, True, math.nan, math.inf, -1, 0, 10 ** 500):
            with self.assertRaises(ValueError):
                usgs.UsgsClient(value)

    def test_coverage_checks_a_real_pixel_without_exposing_sample_values(self):
        result = self.client.coverage(POINT)
        self.assertEqual(result["status"], "available")
        self.assertNotIn("samples", result)
        self.assertEqual([request[0] for request in self.provider.requests], ["query", "info", "getSamples"])
        self.provider.transform = lambda rows, identifier: []
        self.assert_unavailable(self.client.coverage(POINT), "no_coverage")
        self.assert_unavailable(self.client.grade(PLACEMENT), "incomplete_coverage")

    def test_requests_have_fixed_endpoints_bounded_catalog_and_one_locked_source(self):
        self.client.grade(PLACEMENT)
        catalog = self.provider.requests[0][1]
        self.assertEqual(catalog["where"], "Category = 1 AND LowPS > 0 AND LowPS <= 1.01")
        self.assertEqual(int(catalog["resultRecordCount"]), 10)
        self.assertEqual(catalog["orderByFields"], "AcquisitionDate DESC,OBJECTID DESC")
        self.assertEqual(catalog["returnGeometry"], "false")
        sample = self.provider.requests[-1][1]
        self.assertEqual(sample["interpolation"], "RSP_NearestNeighbor")
        self.assertEqual(sample["returnFirstValueOnly"], "false")
        self.assertEqual(json.loads(sample["mosaicRule"]), {"mosaicMethod": "esriMosaicLockRaster", "lockRasterIds": [10]})
        self.assertEqual(len(json.loads(sample["geometry"])["points"]), 4)
        for operation, params, timeout, request in self.provider.requests:
            parsed = urllib.parse.urlparse(request.full_url)
            self.assertEqual(parsed.scheme, "https")
            self.assertEqual(parsed.netloc, "elevation.nationalmap.gov")
            self.assertLessEqual(timeout, usgs.REQUEST_SECONDS)
            self.assertGreater(timeout, 0)
            self.assertEqual(params["f"], "json")

    def test_missing_catalog_and_coarse_or_unsupported_metadata(self):
        self.provider.candidates = []
        self.assert_unavailable(self.client.grade(PLACEMENT), "no_coverage")
        for changes in ({"LowPS": 10}, {"LowPS": 0}, {"LowPS": True}, {"LowPS": math.nan}, {"LowPS": "1"},
                        {"Category": 2}, {"Source": "Unknown"}, {"VerticalDatum": "Unknown"}, {"Name": ""},
                        {"title": None}, {"title": "USGS Original Product Resolution"}, {"title": "USGS 10 Meter"},
                        {"OBJECTID": True}, {"OBJECTID": -1}, {"OBJECTID": "10"}):
            with self.subTest(changes=changes):
                self.provider.candidates = [attributes(**changes)]
                self.assert_unavailable(self.client.grade(PLACEMENT), "unsupported_source")
        self.assertTrue(all(r[0] == "query" for r in self.provider.requests))

    def test_null_or_unreadable_acquisition_date_is_not_invented(self):
        for value in (None, "2021-07-09", math.nan, 10 ** 500):
            self.provider.candidates = [attributes(date=value)]
            result = self.client.grade(PLACEMENT)
            # NaN cannot be equal to itself in provenance, so fail closed.
            if isinstance(value, float) and math.isnan(value):
                self.assert_unavailable(result, "unsupported_source")
            else:
                self.assertEqual(result["sourceDate"], None)

    def test_native_info_rejects_coarse_angular_feet_or_ambiguous_units(self):
        for changes in ({"pixelSizeX": 2}, {"pixelSizeY": 2}, {"pixelSizeX": 0}, {"pixelSizeY": math.nan},
                        {"pixelSizeX": "1"}, {"pixelSizeX": True}, {"bandCount": 2}, {"bandCount": True}, {"pixelType": "U8"},
                        {"origin": None, "extent": None}, {"origin": {"spatialReference": {"wkid": 4326}}},
                        {"origin": {"spatialReference": {"wkid": 3857}}}, {"origin": {"spatialReference": {"wkid": 2230}}},
                        {"origin": {"spatialReference": {"wkid": 26911}}},
                        {"origin": {"spatialReference": {"wkid": 26911, "latestWkid": 26910}}}):
            with self.subTest(changes=changes):
                self.provider.info = native(**changes)
                self.assert_unavailable(self.client.grade(PLACEMENT), "unsupported_source")
        self.assertFalse(any(r[0] == "getSamples" for r in self.provider.requests))

    def test_native_resolution_is_worst_validated_ground_spacing(self):
        self.provider.info = native(pixelSizeX=1.005, pixelSizeY=0.5)
        self.assertEqual(self.client.grade(PLACEMENT)["resolutionMeters"], 1.005)

    def test_metadata_cache_is_bounded_and_expires_without_address_keys(self):
        with patch("rr.usgs.NATIVE_CACHE_SIZE", 2):
            for identifier in (1, 2, 3, 3):
                self.provider.candidates = [attributes(identifier=identifier)]
                self.assertEqual(self.client.coverage(POINT)["status"], "available")
        self.assertEqual(list(self.client._native_cache), [2, 3])
        self.assertEqual(sum(r[0] == "info" for r in self.provider.requests), 3)
        self.client._native_cache[3] = (0, 1.0)
        self.assertEqual(self.client.coverage(POINT)["status"], "available")
        self.assertEqual(sum(r[0] == "info" for r in self.provider.requests), 4)

    def test_no_data_nan_infinity_and_nonnumeric_values_never_become_zero(self):
        for value in (None, "NoData", "nan", "Infinity", math.nan, math.inf, -999999, -32768, "", "1,2", True, 10 ** 500, 9001, -501):
            with self.subTest(value=value):
                self.provider.values = [value] * 4
                self.assert_unavailable(self.client.grade(PLACEMENT), "incomplete_coverage")
        self.provider.values = [-86.0] * 4
        self.assertEqual(self.client.grade(PLACEMENT)["status"], "available")

    def test_missing_duplicate_and_incorrect_location_ids_are_rejected(self):
        transformations = [lambda r: r[:-1], lambda r: r + [r[0]],
                           lambda r: [r[0], r[0], r[2], r[3]],
                           lambda r: [{**r[0], "locationId": 8}] + r[1:],
                           lambda r: [{**r[0], "locationId": "0"}] + r[1:],
                           lambda r: [{**r[0], "locationId": True}] + r[1:]]
        for transform in transformations:
            self.provider.transform = lambda rows, identifier, transform=transform: transform(rows)
            self.assert_unavailable(self.client.grade(PLACEMENT), "incomplete_coverage")
        self.provider.transform = lambda rows, identifier: list(reversed(rows))
        self.assertEqual(self.client.grade(PLACEMENT)["status"], "available")

    def test_wrong_coordinates_and_spatial_references_are_rejected(self):
        for change in ({"x": -122.0}, {"y": 48.0}, {"x": math.nan}, {"x": True}, {"x": 10 ** 500},
                       {"spatialReference": {"wkid": 3857}}, {"spatialReference": None}, {"spatialReference": {"wkid": 4326, "latestWkid": 3857}}):
            def transform(rows, identifier, change=change):
                rows[0]["location"].update(change)
                return rows
            self.provider.transform = transform
            self.assert_unavailable(self.client.grade(PLACEMENT), "incomplete_coverage")

    def test_mixed_raster_source_datum_or_resolution_are_rejected(self):
        changes = [{"rasterId": 11}, {"rasterId": "10"}, {"resolution": 10}, {"resolution": 0}, {"resolution": "1"},
                   {"resolution": math.nan}, {"resolution": True}, {"resolution": 0.5}, {"attributes": None}]
        changes.extend({"attributes": attributes(**change)} for change in ({"Source": "OTHER"}, {"Name": "OTHER"}, {"VerticalDatum": "OTHER"}, {"AcquisitionDate": 0}, {"title": "OTHER"}))
        for change in changes:
            self.provider.transform = lambda rows, identifier, change=change: [{**rows[0], **change}] + rows[1:]
            self.assert_unavailable(self.client.grade(PLACEMENT), "unsupported_source")

    def test_candidates_are_newest_first_and_retried_at_most_three_times(self):
        self.provider.candidates = [attributes(identifier=n, date=n * 1000) for n in (2, 4, 1, 3)]
        self.provider.transform = lambda rows, identifier: []
        self.assert_unavailable(self.client.grade(PLACEMENT), "incomplete_coverage")
        sampled = [json.loads(r[1]["mosaicRule"])["lockRasterIds"][0] for r in self.provider.requests if r[0] == "getSamples"]
        self.assertEqual(sampled, [4, 3, 2])
        self.assertEqual(len(self.provider.requests), 7)

    def test_missing_newest_raster_can_fall_back_to_one_complete_older_source(self):
        self.provider.candidates = [attributes(identifier=20, date=2000), attributes(identifier=10, date=1000)]
        self.provider.transform = lambda rows, identifier: [] if identifier == 20 else rows
        result = self.client.grade(PLACEMENT)
        self.assertEqual(result["status"], "available")
        self.assertEqual(sum(r[0] == "getSamples" for r in self.provider.requests), 2)

    def test_malformed_or_oversized_provider_responses_are_sanitized(self):
        for response in (Response(PRIVATE.encode()), Response([]), Response({"error": {"message": PRIVATE}}),
                         Response({"features": None}), Response({"features": [{}] * 11}),
                         Response(b"x" * (usgs.MAX_RESPONSE_BYTES + 1)),
                         Response({}, headers={"Content-Length": str(usgs.MAX_RESPONSE_BYTES + 1)}),
                         Response({}, headers={"Content-Length": "bad"}), Response({}, headers={"Content-Length": "-1"}),
                         Response({}, content_type="text/html"), Response({}, headers={"Content-Encoding": "gzip"}), Response({}, status=204)):
            self.client._opener.open.side_effect = None
            self.client._opener.open.return_value = response
            self.assert_unavailable(self.client.grade(PLACEMENT), "service_unavailable")
            self.assertTrue(all(size <= usgs.MAX_RESPONSE_BYTES + 1 for size in response.read_sizes))

    def test_missing_sample_schema_is_a_service_failure(self):
        with patch.object(self.client, "_samples", side_effect=usgs._Unavailable("service_unavailable")):
            self.assert_unavailable(self.client.grade(PLACEMENT), "service_unavailable")

    def test_http_redirects_429_tls_and_timeouts_do_not_leak_provider_errors(self):
        errors = [TimeoutError(PRIVATE), urllib.error.URLError(ssl.SSLError(PRIVATE))]
        errors.extend(urllib.error.HTTPError("https://example.invalid/" + PRIVATE, code, PRIVATE, {}, io.BytesIO(PRIVATE.encode())) for code in (301, 302, 307, 308, 429, 500))
        for error in errors:
            self.client._opener.open.reset_mock()
            self.client._opener.open.side_effect = error
            self.assert_unavailable(self.client.grade(PLACEMENT), "service_unavailable")
            self.assertEqual(self.client._opener.open.call_count, 1)
        for code in (301, 302, 303, 307, 308):
            self.assertIsNone(usgs._NoRedirect().redirect_request(None, None, code, "", {}, "https://example.invalid/"))
        client = usgs.UsgsClient()
        https = next(h for h in client._opener.handlers if isinstance(h, urllib.request.HTTPSHandler))
        self.assertEqual(https._context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(https._context.check_hostname)

    def test_deadline_returns_even_if_network_operation_is_stuck(self):
        release, started = threading.Event(), threading.Event()
        slots = threading.BoundedSemaphore(1)
        def blocked(request, timeout):
            started.set()
            release.wait(2)
            return Response({"features": []})
        self.client._opener.open.side_effect = blocked
        with patch("rr.usgs.OPERATION_SECONDS", 0.05), patch("rr.usgs._NETWORK_SLOTS", slots):
            began = time.monotonic()
            try:
                self.assert_unavailable(self.client.grade(PLACEMENT), "service_unavailable")
                self.assertLess(time.monotonic() - began, 0.5)
                self.assertTrue(started.is_set())
                # The occupied worker slot fails closed without spawning more.
                self.assert_unavailable(self.client.coverage(POINT), "service_unavailable")
                self.assertEqual(self.client._opener.open.call_count, 1)
            finally:
                release.set()
                self.assertTrue(slots.acquire(timeout=1))
                slots.release()

    def test_expired_deadline_never_starts_another_request(self):
        with self.assertRaises(usgs._Unavailable):
            self.client._json("query", {}, time.monotonic() - 1)
        self.client._opener.open.assert_not_called()
        for operation in ("../info", "https://example.invalid", "-1/info", "0/info", "01/other", "2147483648/info"):
            with self.assertRaises(usgs._Unavailable):
                self.client._json(operation, {}, time.monotonic() + 1)
        self.client._opener.open.assert_not_called()

    def test_bad_placement_and_location_are_rejected_without_network(self):
        for point in ({"lat": 0, "lng": 0}, {"lat": math.nan, "lng": -122}):
            with self.assertRaises(Problem):
                self.client.coverage(point)
        for changes in ({"containerSize": 99}, {"bearingDegrees": math.inf}, {"bearingDegrees": -1}):
            with self.assertRaises(Problem):
                self.client.grade({**PLACEMENT, **changes})
        self.client._opener.open.assert_not_called()


if __name__ == "__main__":
    unittest.main()
