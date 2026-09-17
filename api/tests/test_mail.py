"""Contract tests for the fixed-recipient boundary, with no outgoing messages."""
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rr.mail import Mailer
from rr.models import Problem


class MailTests(unittest.TestCase):
    def setUp(self):
        self.messages = []
        self.status = "Running"
        owner = self

        class Client:
            @classmethod
            def from_connection_string(cls, *args, **kwargs):
                return cls()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def begin_send(self, message, **kwargs):
                owner.messages.append((message, kwargs))
                if isinstance(owner.status, Exception):
                    raise owner.status
                return types.SimpleNamespace(result=lambda: {"status": owner.status})

        module = types.ModuleType("azure.communication.email")
        module.EmailClient = Client
        self.fake_sdk = patch.dict(sys.modules, {"azure.communication.email": module})
        self.fake_sdk.start()
        self.addCleanup(self.fake_sdk.stop)
        self.mailer = Mailer("test-connection-private", "sender@example.com")

    def test_fixed_destination_plain_text_reply_to_and_provider_operation(self):
        self.mailer.send("Subject", "<script>literal customer text</script>", "customer@example.com", "operation-reference")
        message, options = self.messages[0]
        self.assertEqual(message["recipients"], {"to": [{"address": "jjfeore@gmail.com"}]})
        self.assertEqual(message["replyTo"], [{"address": "customer@example.com"}])
        self.assertNotIn("html", message["content"])
        self.assertEqual(options["operation_id"], "operation-reference")
        self.assertFalse(options["polling"])

    def test_failed_or_unknown_provider_status_not_success(self):
        for status in ("Failed", "Canceled", "Unexpected", None):
            self.status = status
            with self.assertRaises(Problem) as error:
                self.mailer.send("Subject", "Body", "customer@example.com", "id")
            self.assertEqual(error.exception.status, 502)

    def test_provider_exception_is_redacted_and_uncertain(self):
        self.status = RuntimeError("key=test-connection-private&customer=sensitive")
        with self.assertRaises(Problem) as error:
            self.mailer.send("Subject", "Body", "customer@example.com", "id")
        self.assertEqual(error.exception.code, "EMAIL_UNCONFIRMED")
        self.assertNotIn("test-connection-private", str(error.exception))
        self.assertNotIn("sensitive", str(error.exception))

    def test_config_cannot_turn_endpoint_into_email_relay(self):
        mailer = Mailer("configured", "sender@example.com", "attacker@example.com")
        self.assertFalse(mailer.configured)
        with self.assertRaises(Problem):
            mailer.send("Subject", "Body", "customer@example.com", "id")
        self.assertEqual(self.messages, [])


if __name__ == "__main__":
    unittest.main()
