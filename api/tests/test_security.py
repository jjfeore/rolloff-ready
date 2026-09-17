"""Origin and process-local request-limit behavior, without provider calls."""

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from rr.models import Problem
from rr.security import RateLimiter, check_origin


class OriginTests(unittest.TestCase):
    allowed = ("https://rolloff.example",)

    def test_malformed_origins_are_forbidden_not_internal_errors(self):
        for origin in ("https://[bad", "https://rolloff.example\uff0f@attacker.test", ["https://rolloff.example"]):
            with self.subTest(origin=origin), self.assertRaises(Problem) as caught:
                check_origin({"origin": origin}, self.allowed, True)
            self.assertEqual(caught.exception.status, 403)
            self.assertEqual(caught.exception.code, "ORIGIN_NOT_ALLOWED")

    def test_origin_confusion_and_cross_site_requests_are_forbidden(self):
        for origin in ("https://attacker.example", "https://rolloff.example.attacker.test", "https://rolloff.example@attacker.test", "https://rolloff.example/path", "https://rolloff.example?x=y", "null"):
            with self.subTest(origin=origin), self.assertRaises(Problem):
                check_origin({"origin": origin}, self.allowed, True)
        with self.assertRaises(Problem):
            check_origin({"origin": self.allowed[0], "sec-fetch-site": "cross-site"}, self.allowed, True)

    def test_allowed_origin_and_originless_clients(self):
        check_origin({"origin": self.allowed[0], "sec-fetch-site": "same-origin"}, self.allowed, True)
        check_origin({}, self.allowed, True)

    def test_production_without_origin_configuration_fails_closed(self):
        with self.assertRaises(Problem) as caught:
            check_origin({}, (), True)
        self.assertEqual(caught.exception.status, 503)


class RateLimiterTests(unittest.TestCase):
    def test_limit_rejects_then_recovers_at_window_boundary(self):
        now = [100]
        limits = RateLimiter(clock=lambda: now[0])
        limits.take("user", 2, 60)
        limits.take("user", 2, 60)
        with self.assertRaises(Problem) as caught:
            limits.take("user", 2, 60)
        self.assertEqual(caught.exception.status, 429)
        now[0] = 160
        limits.take("user", 2, 60)

    def test_independent_keys_and_global_limit(self):
        limits = RateLimiter(clock=lambda: 100)
        for user in ("one", "two"):
            limits.take(user, 1, 60)
            limits.take("global", 2, 60)
        with self.assertRaises(Problem):
            limits.take("global", 2, 60)
        with self.assertRaises(Problem):
            limits.take("one", 1, 60)


if __name__ == "__main__":
    unittest.main()
