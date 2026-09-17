"""Terrain evidence is advisory, bound to its site, and never trusted from client JSON."""
import copy
import json
from pathlib import Path
import sys
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rr.app import Application, Settings
from rr.security import Tokens


class FakeHere:
    def attribution(self):
        return "HERE"


class FakeMailer:
    configured = True

    def __init__(self):
        self.sent = []

    def send(self, *args):
        self.sent.append(args)


class FakeTerrain:
    def __init__(self):
        self.calls = []
        self.result = {
            "status": "available", "source": "USGS 3DEP", "message": "Four-point terrain estimate.",
            "resolutionMeters": 1, "sourceName": "TEST_DATASET", "sourceDate": "2023-04-20",
            "thresholdPercent": 3, "lateralOffsetMeters": 1.5, "estimatedAt": "2026-09-17T00:00:00Z",
            "along": {"percent": 9, "baselineMeters": 19.873, "riseMeters": 1.78857, "classification": "review", "direction": "uphill"},
            "across": {"percent": -4, "baselineMeters": 5.4384, "riseMeters": -0.217536, "classification": "review", "direction": "left_up"},
            "samples": {name: {"lat": 47.604, "lng": -122.33, "elevationMeters": elevation}
                        for name, elevation in (("containerEnd", 31.78857), ("truckFront", 30), ("left", 30.217536), ("right", 30))},
        }

    def grade(self, placement):
        self.calls.append(("grade", copy.deepcopy(placement)))
        return copy.deepcopy(self.result)

    def coverage(self, point):
        self.calls.append(("coverage", copy.deepcopy(point)))
        return {k: v for k, v in self.result.items()
                if k in ("status", "source", "message", "reason", "resolutionMeters", "sourceName", "sourceDate")}


class TerrainApiTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000
        self.tokens = Tokens("terrain-api-test-secret-32-characters", lambda: self.now)
        self.mailer, self.terrain = FakeMailer(), FakeTerrain()
        self.app = Application(Settings(here_key="secret-do-not-output", app_secret="s" * 40),
                               FakeHere(), self.mailer, self.tokens, self.terrain)
        self.site = {
            "address": {"id": "test-site", "label": "600 4th Ave, Seattle, WA", "countryCode": "USA", "position": {"lat": 47.60395, "lng": -122.3305}},
            "placement": {"lat": 47.60395, "lng": -122.3305, "bearingDegrees": 0, "containerSize": 20},
            "answers": {"space": "yes", "obstructions": "no", "slope": "level", "differentPlane": "no", "streetOverlap": "no"},
        }
        self.refresh_session()

    def refresh_session(self):
        self.headers = {"Content-Type": "application/json", "Origin": "http://localhost:5173", "X-Rolloff-Token": self.tokens.issue()}
        self.site["address"]["verification"] = self.tokens.sign_address(self.site["address"])
        self.now += 3

    def post(self, endpoint, body):
        response = self.app.handle("POST", "/api/" + endpoint, self.headers, json.dumps(body).encode(), "test-client")
        return response.status, json.loads(response.body)

    def evidence(self):
        status, result = self.post("grade", self.site)
        self.assertEqual(status, 200)
        return result["evidenceToken"]

    def contact(self, evidence=None):
        data = {**copy.deepcopy(self.site), "requestId": str(uuid.uuid4()), "website": "",
                "customer": {"name": "Sample Customer", "email": "sample@example.com", "consent": True}}
        if evidence is not None:
            data["terrainEvidence"] = evidence
        return data

    def test_config_does_not_call_elevation_service(self):
        result = self.app.handle("GET", "/api/config")
        self.assertEqual(result.status, 200)
        self.assertTrue(json.loads(result.body)["gradeEnabled"])
        self.assertEqual(self.terrain.calls, [])

    def test_address_coverage_is_separate_from_final_placement_sampling(self):
        status, coverage = self.post("terrain-coverage", {"address": self.site["address"]})
        self.assertEqual(status, 200)
        self.assertEqual(coverage["resolutionMeters"], 1)
        self.assertEqual(self.terrain.calls, [("coverage", self.site["address"]["position"])])
        status, grade = self.post("grade", self.site)
        self.assertEqual(status, 200)
        self.assertEqual(self.terrain.calls[-1], ("grade", self.site["placement"]))
        self.assertEqual(grade["along"]["percent"], 9)
        self.assertIn("evidenceToken", grade)

    def test_forged_address_and_missing_address_never_reach_provider(self):
        forged = copy.deepcopy(self.site["address"])
        forged["position"]["lat"] += 0.01
        for endpoint, data in (("terrain-coverage", {"address": forged}),
                               ("grade", {**self.site, "address": forged}),
                               ("grade", {"placement": self.site["placement"]})):
            with self.subTest(endpoint=endpoint):
                self.assertEqual(self.post(endpoint, data)[0], 400)
        self.assertEqual(self.terrain.calls, [])

    def test_distant_and_invalid_placement_never_reach_provider(self):
        for key, value in (("lat", 48), ("containerSize", 25), ("bearingDegrees", float("nan")), ("lng", True)):
            data = copy.deepcopy(self.site)
            data["placement"][key] = value
            self.assertEqual(self.post("grade", data)[0], 400)
        self.assertEqual(self.terrain.calls, [])

    def test_unavailable_is_signed_without_fabricated_zero_slopes(self):
        self.terrain.result = {"status": "unavailable", "reason": "incomplete_coverage", "source": "USGS 3DEP", "message": "Missing elevation samples."}
        status, grade = self.post("grade", self.site)
        self.assertEqual(status, 200)
        self.assertNotIn("along", grade)
        self.assertNotIn("across", grade)
        data = self.contact(grade["evidenceToken"])
        self.assertEqual(self.post("contact", data)[0], 202)
        self.assertIn("Missing elevation samples.", self.mailer.sent[0][1])
        self.assertNotIn("Lengthwise:", self.mailer.sent[0][1])

    def test_outage_and_no_coverage_remain_distinct(self):
        for reason in ("service_unavailable", "no_coverage"):
            self.terrain.result = {"status": "unavailable", "source": "USGS 3DEP", "reason": reason, "message": reason}
            self.assertEqual(self.post("terrain-coverage", {"address": self.site["address"]})[1]["reason"], reason)

    def test_email_uses_signed_evidence_not_client_claims(self):
        data = self.contact(self.evidence())
        data["terrain"] = {"sourceName": "FORGED_DATASET", "along": {"percent": 0}}
        data["grade"] = {"along": {"percent": 0}}
        self.assertEqual(self.post("contact", data)[0], 202)
        body = self.mailer.sent[0][1]
        for expected in ("TEST_DATASET", "+9.0%", "-4.0%", "Container far end", "Source: USGS 3DEP", "not an equipment safety limit"):
            self.assertIn(expected, body)
        self.assertNotIn("FORGED_DATASET", body)

    def test_advisory_does_not_change_user_answers_or_manual_outcome(self):
        status, result = self.post("assess", {**self.site, "terrainEvidence": self.evidence()})
        self.assertEqual(status, 200)
        self.assertEqual(result["outcome"], "likely_suitable")
        self.assertEqual(self.site["answers"]["slope"], "level")

    def test_low_estimate_cannot_override_a_reported_hazard(self):
        self.terrain.result["along"].update(percent=0, classification="low", direction="level")
        self.terrain.result["across"].update(percent=0, classification="low", direction="level")
        self.site["answers"]["slope"] = "sideways"
        status, result = self.post("assess", {**self.site, "terrainEvidence": self.evidence()})
        self.assertEqual(status, 200)
        self.assertEqual(result["outcome"], "review_needed")

    def test_tampered_token_rejected_before_mail(self):
        token = self.evidence()
        replacement = "0" if token[-1] != "0" else "1"
        status, result = self.post("contact", self.contact(token[:-1] + replacement))
        self.assertEqual(status, 400)
        self.assertEqual(result["error"]["code"], "TERRAIN_EVIDENCE_INVALID")
        self.assertEqual(self.mailer.sent, [])

    def test_changed_location_rotation_or_size_cannot_reuse_evidence(self):
        token = self.evidence()
        for field, value in (("lat", 47.60396), ("bearingDegrees", 5), ("containerSize", 30)):
            data = {**copy.deepcopy(self.site), "terrainEvidence": token}
            data["placement"][field] = value
            status, result = self.post("assess", data)
            self.assertEqual(status, 400)
            self.assertEqual(result["error"]["code"], "TERRAIN_EVIDENCE_INVALID")

    def test_changed_address_cannot_reuse_evidence(self):
        token = self.evidence()
        self.site["address"]["id"] = "another-geocoded-site"
        self.refresh_session()
        status, result = self.post("assess", {**self.site, "terrainEvidence": token})
        self.assertEqual(status, 400)
        self.assertEqual(result["error"]["code"], "TERRAIN_EVIDENCE_INVALID")

    def test_old_terrain_evidence_expires_even_with_a_fresh_address_session(self):
        token = self.evidence()
        self.now += 7201
        self.refresh_session()
        status, result = self.post("contact", self.contact(token))
        self.assertEqual(status, 400)
        self.assertEqual(result["error"]["code"], "TERRAIN_EVIDENCE_EXPIRED")
        self.assertEqual(self.mailer.sent, [])

    def test_unsigned_data_does_not_claim_provider_evidence(self):
        data = self.contact()
        data["terrain"] = self.terrain.result
        self.assertEqual(self.post("contact", data)[0], 202)
        self.assertIn("No verified elevation estimate was attached", self.mailer.sent[0][1])
        self.assertNotIn("TEST_DATASET", self.mailer.sent[0][1])

    def test_email_retries_reuse_the_same_verified_snapshot(self):
        data = self.contact(self.evidence())
        self.assertEqual(self.post("contact", data)[0], 202)
        self.assertEqual(self.post("contact", data)[0], 200)
        self.assertEqual(len(self.mailer.sent), 1)
        self.assertEqual(sum(call[0] == "grade" for call in self.terrain.calls), 1)

    def test_coverage_requests_have_a_bounded_per_client_rate(self):
        for _ in range(12):
            self.assertEqual(self.post("terrain-coverage", {"address": self.site["address"]})[0], 200)
        self.assertEqual(self.post("terrain-coverage", {"address": self.site["address"]})[0], 429)
        self.assertEqual(len(self.terrain.calls), 12)


if __name__ == "__main__":
    unittest.main()
