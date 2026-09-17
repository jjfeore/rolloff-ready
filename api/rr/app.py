"""Transport-independent API dispatcher, shared by Azure Functions and local dev."""

import hashlib
import hmac
import json
import os
import re
import secrets
import uuid
from dataclasses import dataclass, field

from .here import HereClient
from .usgs import UsgsClient
from .mail import Mailer
from .models import Problem, assess, assessment_input, customer, email_content, obj, selected_address, site_input, text
from .security import RateLimiter, Requests, Tokens, check_origin


@dataclass
class Settings:
    here_key: str = field(default="", repr=False)
    app_secret: str = field(default="", repr=False)
    connection_string: str = field(default="", repr=False)
    sender: str = ""
    recipient: str = "jjfeore@gmail.com"
    production: bool = False
    origins: tuple = ("http://localhost:5173", "http://127.0.0.1:5173", "http://localhost:7071", "http://127.0.0.1:7071")

    @classmethod
    def from_env(cls):
        prod = os.environ.get("APP_ENV") == "production"
        origins = tuple(x.strip().rstrip("/") for x in os.environ.get("ALLOWED_ORIGINS", "").split(",") if x.strip())
        return cls(here_key=os.environ.get("HERE_MAPS_API_KEY", ""),
                   app_secret=os.environ.get("APP_SECRET", ""),
                   connection_string=os.environ.get("ACS_CONNECTION_STRING", ""),
                   sender=os.environ.get("EMAIL_SENDER", ""),
                   recipient=os.environ.get("CONTACT_RECIPIENT", "jjfeore@gmail.com"),
                   production=prod, origins=origins if prod or origins else cls.origins)


@dataclass
class Response:
    status: int
    body: bytes
    headers: dict


def json_response(status, value):
    return Response(status, json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"), {
        "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff", "Referrer-Policy": "same-origin",
    })


class Application:
    def __init__(self, settings=None, here=None, mailer=None, tokens=None, terrain=None):
        self.settings = settings or Settings.from_env()
        self.here = here or HereClient(self.settings.here_key)
        self.terrain = terrain or UsgsClient()
        self.mailer = mailer or Mailer(self.settings.connection_string, self.settings.sender, self.settings.recipient)
        self.tokens = tokens or Tokens(self.settings.app_secret or secrets.token_urlsafe(48))
        self.limits, self.requests = RateLimiter(), Requests()

    def handle(self, method, path, headers=None, body=b"", client_ip="unknown"):
        try:
            return self.dispatch(method.upper(), path.split("?", 1)[0], {k.lower(): v for k, v in (headers or {}).items()}, body, client_ip)
        except Problem as problem:
            result = json_response(problem.status, {"error": {"code": problem.code, "message": problem.message}})
            if problem.status == 429:
                result.headers["Retry-After"] = "60"
            return result
        except Exception:
            # Log only a fixed category through the host, never provider exceptions
            # or bodies. Tests target specific failure branches before this boundary.
            return json_response(500, {"error": {"code": "INTERNAL_ERROR", "message": "Something went wrong. Your details have not been cleared; please try again."}})

    def dispatch(self, method, path, headers, body, client_ip):
        if self.settings.production and len(self.settings.app_secret) < 32:
            raise Problem(503, "SERVICE_NOT_CONFIGURED", "The service is being configured. Please try again later.")
        check_origin(headers, self.settings.origins, self.settings.production)
        client = hashlib.sha256(str(client_ip).encode()).hexdigest()[:24]
        self.limits.take("global", 3000, 60)
        if path == "/api/config" and method == "GET":
            self.limits.take(("config", client), 30, 60)
            return json_response(200, {"mapsConfigured": bool(self.settings.here_key),
                                      "emailConfigured": self.mailer.configured,
                                      "gradeEnabled": True, "formToken": self.tokens.issue(),
                                      "attribution": self.here.attribution()})
        match = re.fullmatch(r"/api/tiles/(\d{1,2})/(\d{1,7})/(\d{1,7})", path)
        if match and method == "GET":
            self.limits.take(("tiles", client), 360, 60)
            z, x, y = map(int, match.groups())
            if not 14 <= z <= 20 or not 0 <= x < 2 ** z or not 0 <= y < 2 ** z:
                raise Problem(400, "INVALID_TILE", "This map tile is outside the supported range.")
            image, content_type = self.here.tile(z, x, y)
            return Response(200, image, {"Content-Type": content_type, "Cache-Control": "private, max-age=3600", "X-Content-Type-Options": "nosniff"})
        allowed = {"/api/geocode", "/api/terrain-coverage", "/api/grade", "/api/assess", "/api/contact"}
        if path not in allowed:
            raise Problem(404, "NOT_FOUND", "This endpoint does not exist.")
        if method != "POST":
            raise Problem(405, "METHOD_NOT_ALLOWED", "Use POST for this request.")
        if len(body) > 16384:
            raise Problem(413, "REQUEST_TOO_LARGE", "This request is too large. Shorten your notes and try again.")
        if headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise Problem(415, "JSON_REQUIRED", "Send this request as JSON.")
        token_age = self.tokens.verify(headers.get("x-rolloff-token"))
        try:
            def reject_constant(_):
                raise ValueError()
            data = json.loads(body.decode("utf-8"), parse_constant=reject_constant)
        except (ValueError, UnicodeDecodeError, RecursionError):
            raise Problem(400, "INVALID_JSON", "This request could not be read.") from None
        obj(data, "Request")
        if path == "/api/geocode":
            self.limits.take(("geocode", client), 20, 60)
            query = text(data.get("query"), "a US street address", 200, 8)
            items = self.here.geocode(query)
            return json_response(200, {"items": [{**item, "verification": self.tokens.sign_address(item)} for item in items]})
        if path == "/api/terrain-coverage":
            self.limits.take(("coverage", client), 12, 60)
            self.limits.take("all-terrain", 120, 60)
            address = selected_address(data.get("address"))
            self.tokens.verify_address(address)
            return json_response(200, self.terrain.coverage(address["position"]))
        if path == "/api/grade":
            self.limits.take(("grade", client), 12, 60)
            self.limits.take("all-terrain", 120, 60)
            site = site_input(data)
            self.tokens.verify_address(site["address"])
            grade = self.terrain.grade(site["placement"])
            return json_response(200, {**grade, "evidenceToken": self.tokens.sign_terrain(site["address"], site["placement"], grade)})
        validated = assessment_input(data)
        self.tokens.verify_address(validated["address"])
        if data.get("terrainEvidence") is not None:
            validated["terrain"] = self.tokens.verify_terrain(data["terrainEvidence"], validated["address"], validated["placement"])
        result = assess(validated)
        if path == "/api/assess":
            self.limits.take(("assess", client), 60, 60)
            return json_response(200, result)
        person = customer(data.get("customer"))
        if data.get("website", "") != "":
            raise Problem(400, "INVALID_INPUT", "This request could not be accepted.")
        if token_age < 2:
            raise Problem(429, "FORM_TOO_FAST", "Please take a moment to review the details, then send your request.")
        try:
            request_id = str(uuid.UUID(data.get("requestId", "")))
        except (ValueError, TypeError, AttributeError):
            raise Problem(400, "INVALID_INPUT", "Refresh the request form before sending.") from None
        fingerprint = hashlib.sha256(json.dumps({"site": validated, "customer": person}, sort_keys=True).encode()).hexdigest()
        previous = self.requests.start(request_id, fingerprint)
        if previous:
            return json_response(200, previous)
        try:
            self.limits.take(("contact", client), 3, 300)
            self.limits.take("all-contact", 30, 3600)
            subject, message = email_content(validated, person, result, request_id)
            # A deterministic provider operation ID supports retries across workers;
            # the local guard alone must not be described as exactly-once delivery.
            key = self.settings.app_secret or self.tokens.secret.decode()
            digest = hmac.new(key.encode(), (request_id + fingerprint).encode(), hashlib.sha256).digest()
            operation_id = str(uuid.UUID(bytes=digest[:16], version=4))
            self.mailer.send(subject, message, person["email"], operation_id)
            response = {"status": "accepted", "requestId": request_id,
                        "message": "Your request was accepted for email delivery. Keep this reference with your site summary."}
            self.requests.finish(request_id, fingerprint, response)
            return json_response(202, response)
        except Exception:
            self.requests.release(request_id)
            raise
