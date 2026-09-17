import copy
import json
from pathlib import Path
import sys
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rr.app import Application, Settings
from rr.mail import Mailer
from rr.models import Problem, assessment_input, assess, distance_m, email_content, customer
from rr.security import RateLimiter, Tokens


SITE = {
    "address": {"id": "public-test-address", "label": "600 4th Ave, Seattle, WA 98104, United States",
                "countryCode": "USA", "position": {"lat": 47.60395, "lng": -122.3305}},
    "placement": {"lat": 47.60395, "lng": -122.3305, "bearingDegrees": 90, "containerSize": 20},
    "answers": {"space": "yes", "obstructions": "no", "slope": "level", "differentPlane": "no", "streetOverlap": "no"},
}
PERSON = {"name": "Test Customer", "email": "customer@example.com", "phone": "+1 (206) 555-0100", "notes": "A renovation project.", "consent": True}


class FakeHere:
    def __init__(self):
        self.queries = []

    def attribution(self):
        return "© HERE, Maxar"

    def geocode(self, query):
        self.queries.append(query)
        return [SITE["address"]]

    def tile(self, z, x, y):
        return b"fake-image-for-unit-test", "image/jpeg"


class FakeTerrain:
    def grade(self, p):
        return {"status": "unavailable", "reason": "no_coverage", "message": "No verified 1-meter terrain data.", "source": "USGS 3DEP"}

    def coverage(self, p):
        return self.grade(p)


class FakeMailer:
    configured = True

    def __init__(self):
        self.sent = []
        self.failure = None

    def send(self, *args):
        if self.failure:
            raise self.failure
        self.sent.append(args)


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000
        self.tokens = Tokens("unit-test-secret-do-not-use-in-production", lambda: self.now)
        self.mailer, self.here = FakeMailer(), FakeHere()
        self.settings = Settings(here_key="test-key-MUST-NOT-BE-RETURNED", app_secret="test-secret-is-at-least-thirty-two-characters")
        self.app = Application(self.settings, self.here, self.mailer, self.tokens, terrain=FakeTerrain())
        self.headers = {"Content-Type": "application/json", "Origin": "http://localhost:5173", "X-Rolloff-Token": self.tokens.issue()}
        self.now += 10
        SITE["address"]["verification"] = self.tokens.sign_address(SITE["address"])

    def post(self, endpoint, data, headers=None):
        return self.app.handle("POST", "/api/" + endpoint, self.headers if headers is None else headers, json.dumps(data).encode(), "127.0.0.1")

    def decode(self, response):
        return json.loads(response.body)

    def contact(self):
        return {**copy.deepcopy(SITE), "customer": copy.deepcopy(PERSON), "website": "", "requestId": str(uuid.uuid4())}

    def test_config_never_exposes_secrets(self):
        response = self.app.handle("GET", "/api/config")
        self.assertEqual(response.status, 200)
        self.assertNotIn(self.settings.here_key.encode(), response.body)
        self.assertNotIn(self.settings.app_secret.encode(), response.body)
        self.assertTrue(self.decode(response)["gradeEnabled"])

    def test_clear_answers_positive(self):
        response = self.post("assess", SITE)
        self.assertEqual(response.status, 200)
        self.assertEqual(self.decode(response)["outcome"], "likely_suitable")

    def test_every_unknown_or_hazard_requires_review(self):
        cases = [("space", "no"), ("space", "unsure"), ("obstructions", "yes"), ("obstructions", "unsure"),
                 ("slope", "sideways"), ("slope", "unsure"), ("differentPlane", "yes"), ("differentPlane", "unsure"),
                 ("streetOverlap", "yes"), ("streetOverlap", "unsure")]
        for field, answer in cases:
            with self.subTest(field=field, answer=answer):
                data = copy.deepcopy(SITE)
                data["answers"][field] = answer
                self.assertEqual(self.decode(self.post("assess", data))["outcome"], "review_needed")

    def test_inline_direction_is_required_and_direction_specific(self):
        data = copy.deepcopy(SITE)
        data["answers"]["slope"] = "inline"
        self.assertEqual(self.post("assess", data).status, 400)
        for direction in ("uphill", "downhill", "unsure"):
            data["answers"]["inlineDirection"] = direction
            response = self.post("assess", data)
            self.assertEqual(self.decode(response)["outcome"], "review_needed")
            if direction != "unsure":
                self.assertIn(direction, self.decode(response)["reasons"][0])

    def test_missing_answers_cannot_pass(self):
        for field in SITE["answers"]:
            data = copy.deepcopy(SITE)
            del data["answers"][field]
            self.assertEqual(self.post("assess", data).status, 400)

    def test_country_and_distant_placement_rejected(self):
        data = copy.deepcopy(SITE)
        data["address"]["countryCode"] = "CAN"
        self.assertEqual(self.post("assess", data).status, 400)
        data = copy.deepcopy(SITE)
        data["placement"]["lat"] += 0.01
        self.assertEqual(self.post("assess", data).status, 400)

    def test_nan_bool_and_invalid_container_rejected(self):
        for field, value in (("lat", float("nan")), ("lat", True), ("lat", 10 ** 400), ("containerSize", True), ("containerSize", 25), ("bearingDegrees", -1)):
            data = copy.deepcopy(SITE)
            data["placement"][field] = value
            self.assertEqual(self.post("assess", data).status, 400)

    def test_forged_country_flag_cannot_bypass_geocode_verification(self):
        data = copy.deepcopy(SITE)
        data["address"]["label"] = "Vancouver BC"
        data["address"]["position"] = {"lat": 49.2827, "lng": -123.1207}
        data["placement"].update(lat=49.2827, lng=-123.1207)
        response = self.post("assess", data)
        self.assertEqual(response.status, 400)
        self.assertEqual(self.decode(response)["error"]["code"], "ADDRESS_NOT_VERIFIED")

    def test_cross_origin_cannot_use_api_even_with_token(self):
        headers = {**self.headers, "Origin": "https://malicious.example"}
        self.assertEqual(self.post("assess", SITE, headers).status, 403)
        headers["Origin"] = "http://localhost:5173.evil.example"
        self.assertEqual(self.post("assess", SITE, headers).status, 403)

    def test_missing_forged_and_expired_tokens(self):
        for token in (None, "invalid", self.headers["X-Rolloff-Token"] + "x"):
            self.assertEqual(self.post("assess", SITE, {**self.headers, "X-Rolloff-Token": token}).status, 403)
        self.now += 7201
        self.assertEqual(self.decode(self.post("assess", SITE))["error"]["code"], "TOKEN_EXPIRED")

    def test_non_json_body_and_oversized_body(self):
        self.assertEqual(self.post("assess", SITE, {**self.headers, "Content-Type": "text/plain"}).status, 415)
        self.assertEqual(self.app.handle("POST", "/api/assess", self.headers, b"a" * 16385).status, 413)
        self.assertEqual(self.app.handle("POST", "/api/assess", self.headers, b"{").status, 400)
        self.assertEqual(self.post("assess", []).status, 400)

    def test_geocode_proxied_not_authenticated_url(self):
        response = self.post("geocode", {"query": "600 4th Ave Seattle WA"})
        self.assertEqual(response.status, 200)
        self.assertEqual(len(self.here.queries), 1)
        self.assertNotIn(self.settings.here_key.encode(), response.body)
        self.assertEqual(self.post("geocode", {"query": "LA"}).status, 400)

    def test_tile_indexes_and_method_bounded(self):
        for path in ("/api/tiles/30/1/1", "/api/tiles/14/16384/1"):
            self.assertEqual(self.app.handle("GET", path).status, 400)
        self.assertEqual(self.app.handle("GET", "/api/geocode").status, 405)
        self.assertEqual(self.app.handle("GET", "/api/unknown").status, 404)

    def test_grade_unavailable_does_not_become_zero(self):
        result = self.decode(self.post("grade", {"address": SITE["address"], "placement": SITE["placement"]}))
        self.assertEqual(result["status"], "unavailable")
        self.assertNotIn("streetGradePercent", result)

    def test_contact_recomputes_result_and_includes_site_fields(self):
        data = self.contact()
        data["assessment"] = {"outcome": "likely_suitable"}
        data["to"] = "attacker@example.com"
        data["answers"]["slope"] = "sideways"
        response = self.post("contact", data)
        self.assertEqual(response.status, 202)
        subject, body, reply_to, operation_id = self.mailer.sent[0]
        self.assertIn("Delivery review", subject)
        self.assertIn("sideways", body)
        self.assertIn("90.0 degrees", body)
        self.assertIn("43.2", body)
        self.assertNotIn("attacker@example.com", body)
        self.assertEqual(reply_to, PERSON["email"])
        uuid.UUID(operation_id)

    def test_contact_duplicate_does_not_send_twice(self):
        data = self.contact()
        first, second = self.post("contact", data), self.post("contact", data)
        self.assertEqual(first.status, 202)
        self.assertEqual(second.status, 200)
        self.assertEqual(len(self.mailer.sent), 1)
        self.assertEqual(self.decode(first), self.decode(second))
        data["customer"]["notes"] = "Changed request"
        self.assertEqual(self.post("contact", data).status, 409)

    def test_email_failure_never_claims_success(self):
        data = self.contact()
        self.mailer.failure = Problem(502, "EMAIL_FAILED", "Unavailable")
        self.assertEqual(self.post("contact", data).status, 502)
        self.assertEqual(len(self.mailer.sent), 0)
        self.mailer.failure = None
        self.assertEqual(self.post("contact", data).status, 202)

    def test_unconfigured_email_is_honest(self):
        self.app.mailer = Mailer("", "")
        response = self.post("contact", self.contact())
        self.assertEqual(response.status, 503)
        self.assertEqual(self.decode(response)["error"]["code"], "EMAIL_NOT_CONFIGURED")

    def test_honeypot_consent_and_reply_to_injection(self):
        data = self.contact()
        data["website"] = "https://bot.example"
        self.assertEqual(self.post("contact", data).status, 400)
        data["website"] = ""
        data["customer"]["consent"] = False
        self.assertEqual(self.post("contact", data).status, 400)
        data["customer"]["consent"] = True
        data["customer"]["email"] = "test@example.com\nBcc: injected@example.com"
        self.assertEqual(self.post("contact", data).status, 400)

    def test_rate_limit(self):
        now = [100]
        limiter = RateLimiter(lambda: now[0])
        limiter.take("key", 1, 60)
        with self.assertRaises(Problem):
            limiter.take("key", 1, 60)
        now[0] += 61
        limiter.take("key", 1, 60)

    def test_production_requires_secret_and_allowed_origin_config(self):
        app = Application(Settings(production=True), self.here, self.mailer)
        self.assertEqual(app.handle("GET", "/api/config").status, 503)
        app = Application(Settings(production=True, app_secret="s" * 40, origins=()), self.here, self.mailer)
        self.assertEqual(app.handle("GET", "/api/config").status, 503)

    def test_distance_handles_antimeridian(self):
        a, b = {"lat": 52, "lng": 179.9999}, {"lat": 52, "lng": -179.9999}
        self.assertLess(distance_m(a, b), 20)


if __name__ == "__main__":
    unittest.main()
