"""Provider contract tests; all network responses are mocked."""

import datetime
import io
import json
import math
import pathlib
import ssl
import sys
import unittest
import urllib.error
import urllib.parse
from unittest.mock import Mock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from rr.here import HereClient, _NoRedirect, GEOCODE_MAX_BYTES, TILE_MAX_BYTES
from rr.models import Problem


SECRET = "mock-private-key-do-not-return"


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

    def read(self, size):
        self.read_sizes.append(size)
        return self.body[:size]


def address(**changes):
    data = {"id": "here:test:seattle", "title": "Seattle address", "resultType": "houseNumber",
            "address": {"countryCode": "USA", "label": "600 4th Ave, Seattle, WA"},
            "position": {"lat": 47.60395, "lng": -122.3305}}
    data.update(changes)
    return data


def copyright(labels=("Maxar",)):
    return {"resources": {"base": {"styles": {"satellite.day": ["sat"]}}},
            "copyrights": {"sat": [{"label": label, "minLevel": 0, "maxLevel": 20} for label in labels]}}


class HereClientTests(unittest.TestCase):
    def setUp(self):
        self.client = HereClient(SECRET)
        self.client._opener = Mock()

    def respond(self, response):
        self.client._opener.open.return_value = response

    def test_geocode_filters_countries_types_and_invalid_coordinates(self):
        self.respond(Response({"items": [
            address(), address(id="canada", address={"countryCode": "CAN", "label": "Vancouver"}),
            address(id="city", resultType="locality"), address(id="bad", position={"lat": math.nan, "lng": -122}),
            address(id="huge", position={"lat": 10 ** 400, "lng": -122}),
            address(id="wrong-position", position={"lat": 0, "lng": 0}),
            address(id="street", resultType="street"),
        ]}))
        result = self.client.geocode("600 4th Ave Seattle")
        self.assertEqual([item["id"] for item in result], ["here:test:seattle", "street"])
        self.assertEqual(set(result[0]), {"id", "label", "position", "countryCode"})
        request = self.client._opener.open.call_args.args[0]
        parsed = urllib.parse.urlparse(request.full_url)
        query = urllib.parse.parse_qs(parsed.query)
        self.assertEqual(parsed.netloc, "geocode.search.hereapi.com")
        self.assertEqual(query["in"], ["countryCode:USA"])
        self.assertEqual(query["limit"], ["5"])
        self.assertEqual(self.client._opener.open.call_args.kwargs["timeout"], 12)
        self.assertNotIn(SECRET, json.dumps(result))

    def test_geocode_does_not_return_provider_echo_of_credentials(self):
        self.respond(Response({"items": [address(address={"countryCode": "USA", "label": SECRET})]}))
        self.assertEqual(self.client.geocode("Seattle address"), [])

    def test_bad_query_is_rejected_without_network(self):
        for query in (None, "a", "x" * 251, "a\nb"):
            with self.subTest(query=query), self.assertRaises(Problem) as caught:
                self.client.geocode(query)
            self.assertEqual(caught.exception.status, 400)
        self.client._opener.open.assert_not_called()

    def test_explicit_canadian_endings_are_rejected_before_us_approximation(self):
        # HERE may return Vancouver, Washington when the input explicitly names
        # Vancouver, Canada. Never send those inputs to the US-filtered search.
        for query in (
            "453 W 12th Ave, Vancouver, BC, Canada",
            "453 W 12th Ave Vancouver Canada",
            "453 W 12th Ave, Vancouver, BC, cAnAdA.  ",
            "453 W 12th Ave, Vancouver, BC",
            "453 W 12th Ave, Vancouver, bc, ",
            "453 W 12th Ave, Vancouver V5Y 1V4",
            "453 W 12th Ave, Vancouver V5Y1V4",
            "100 Queen St, Toronto, ON M5H 2N2",
            "100 Queen St, Toronto, ON",
            "100 Main St, Montreal, QC",
        ):
            with self.subTest(query=query), self.assertRaises(Problem) as caught:
                self.client.geocode(query)
            self.assertEqual(caught.exception.status, 400)
            self.assertEqual(caught.exception.code, "US_ONLY")
            self.assertNotIn(query, caught.exception.message)
        self.client._opener.open.assert_not_called()

    def test_us_names_and_streets_containing_canada_are_preserved(self):
        self.respond(Response({"items": [address()]}))
        for query in (
            "123 Canada Road, Woodside, CA 94062",
            "123 Canada Rd",
            "123 Canada Road",
            "453 W 12th St, Vancouver, WA 98660",
            "100 Main St, Ontario, CA 91761",
            "100 Main St, Canadian, TX 79014",
            "123 On Street, Seattle, WA",
        ):
            with self.subTest(query=query):
                self.assertEqual(len(self.client.geocode(query)), 1)
                request = self.client._opener.open.call_args.args[0]
                params = urllib.parse.parse_qs(urllib.parse.urlparse(request.full_url).query)
                self.assertEqual(params["q"], [query])
                self.assertEqual(params["in"], ["countryCode:USA"])
        self.assertEqual(self.client._opener.open.call_count, 7)

    def test_tile_is_fixed_jpeg_and_bounded(self):
        response = Response(b"\xff\xd8\xfftest\xff\xd9", "image/jpeg")
        self.respond(response)
        body, kind = self.client.tile(19, 83984, 183103)
        self.assertEqual(kind, "image/jpeg")
        self.assertTrue(body.startswith(b"\xff\xd8\xff"))
        request = self.client._opener.open.call_args.args[0]
        self.assertEqual(urllib.parse.urlparse(request.full_url).path, "/v3/base/mc/19/83984/183103/jpeg")
        self.assertEqual(response.read_sizes, [TILE_MAX_BYTES + 1])

    def test_invalid_and_non_us_tiles_do_not_fetch(self):
        for values in [(True, 1, 1), (13, 1, 1), (21, 1, 1), (19, -1, 1), (19, 2 ** 19, 1), (19, 1, 2 ** 19), (19, 262144, 262144)]:
            with self.subTest(values=values), self.assertRaises(Problem):
                self.client.tile(*values)
        self.client._opener.open.assert_not_called()

    def test_wrong_image_type_or_signature_is_rejected(self):
        for response in [Response(b"not-jpeg", "image/jpeg"), Response(b"\xff\xd8\xff", "text/html")]:
            self.respond(response)
            with self.assertRaises(Problem) as caught:
                self.client.tile(19, 83984, 183103)
            self.assertEqual(caught.exception.code, "HERE_BAD_RESPONSE")

    def test_oversized_body_and_content_length_are_rejected(self):
        for response in [Response(b"x" * (GEOCODE_MAX_BYTES + 1)), Response(b"{}", headers={"Content-Length": str(GEOCODE_MAX_BYTES + 1)})]:
            self.respond(response)
            with self.assertRaises(Problem) as caught:
                self.client.geocode("Seattle address")
            self.assertEqual(caught.exception.code, "HERE_RESPONSE_TOO_LARGE")

    def test_malformed_json_is_sanitized(self):
        self.respond(Response(SECRET.encode()))
        with self.assertRaises(Problem) as caught:
            self.client.geocode("Seattle address")
        self.assertEqual(caught.exception.code, "HERE_BAD_RESPONSE")
        self.assertNotIn(SECRET, str(caught.exception))

    def test_deeply_nested_provider_json_is_sanitized(self):
        self.respond(Response(b'{"items":[]}'))
        with patch("rr.here.json.loads", side_effect=RecursionError()), self.assertRaises(Problem) as caught:
            self.client.geocode("Seattle address")
        self.assertEqual(caught.exception.code, "HERE_BAD_RESPONSE")

    def test_provider_http_failures_never_expose_urls_or_bodies(self):
        for status in (301, 302, 401, 403, 429, 500):
            self.client._opener.open.side_effect = urllib.error.HTTPError(
                f"https://maps.hereapi.com/?apiKey={SECRET}", status, SECRET, {}, io.BytesIO(SECRET.encode()))
            with self.subTest(status=status), self.assertRaises(Problem) as caught:
                self.client.geocode("Seattle address")
            self.assertNotIn(SECRET, str(caught.exception))
            self.assertNotIn("http", caught.exception.message)
            self.assertTrue(caught.exception.__suppress_context__)

    def test_timeouts_and_tls_failures_are_sanitized(self):
        for error, code in [(TimeoutError(SECRET), "HERE_TIMEOUT"),
                            (urllib.error.URLError(TimeoutError(SECRET)), "HERE_TIMEOUT"),
                            (urllib.error.URLError(ssl.SSLError(SECRET)), "HERE_UNAVAILABLE")]:
            self.client._opener.open.side_effect = error
            with self.assertRaises(Problem) as caught:
                self.client.geocode("Seattle address")
            self.assertEqual(caught.exception.code, code)
            self.assertNotIn(SECRET, caught.exception.message)

    def test_redirect_handler_blocks_all_redirects(self):
        handler = _NoRedirect()
        for code in (301, 302, 303, 307, 308):
            self.assertIsNone(handler.redirect_request(None, None, code, "", {}, "https://attacker.invalid/"))

    def test_attribution_refreshes_after_24_hours(self):
        self.client._opener.open.side_effect = [Response(copyright()), Response(copyright(("New Supplier",)))]
        with patch("rr.here.time.monotonic", side_effect=[100, 86499, 86500]):
            first = self.client.attribution()
            self.assertEqual(self.client.attribution(), first)
            self.assertIn("New Supplier", self.client.attribution())
        self.assertIn("Maxar", first)
        self.assertEqual(self.client._opener.open.call_count, 2)

    def test_attribution_failure_uses_verified_fallback_then_retries(self):
        self.client._opener.open.side_effect = [TimeoutError(SECRET), Response(copyright(("Updated Supplier",)))]
        with patch("rr.here.time.monotonic", side_effect=[10, 69, 70]):
            self.assertIn("HERE, Maxar", self.client.attribution())
            self.assertIn("HERE, Maxar", self.client.attribution())
            self.assertIn("Updated Supplier", self.client.attribution())
        self.assertEqual(self.client._opener.open.call_count, 2)

    def test_attribution_preserves_last_valid_provider_on_failed_refresh(self):
        self.client._opener.open.side_effect = [Response(copyright(("Prior Supplier",))), Response({"resources": None})]
        with patch("rr.here.time.monotonic", side_effect=[0, 86400]):
            first = self.client.attribution()
            self.assertEqual(self.client.attribution(), first)

    def test_grade_never_fetches_or_invents_measurement(self):
        grade = self.client.grade({"lat": 47.6, "lng": -122.3})
        self.assertEqual(grade["status"], "unavailable")
        self.assertEqual(grade["source"], "HERE Map Attributes")
        self.assertNotIn("gradePercent", grade)
        self.client._opener.open.assert_not_called()


if __name__ == "__main__":
    unittest.main()
