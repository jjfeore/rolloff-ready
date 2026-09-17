"""Fixed-recipient email adapter; SDK success means acceptance, not inbox delivery."""

from .models import Problem


class Mailer:
    def __init__(self, connection_string, sender, recipient="jjfeore@gmail.com"):
        self.connection_string, self.sender, self.recipient = connection_string, sender, recipient

    @property
    def configured(self):
        return bool(self.connection_string and self.sender and self.recipient == "jjfeore@gmail.com")

    def send(self, subject, body, reply_to, operation_id):
        if not self.configured:
            raise Problem(503, "EMAIL_NOT_CONFIGURED", "Sending is not available yet. Download your site summary so you can keep it.")
        try:
            from azure.communication.email import EmailClient
            # A fresh client avoids shared mutable transport state between requests.
            # No background polling: begin_send returning 202 is queue acceptance.
            with EmailClient.from_connection_string(
                self.connection_string, connection_timeout=4, read_timeout=10,
                retry_total=0, logging_enable=False,
            ) as client:
                poller = client.begin_send({
                    "senderAddress": self.sender,
                    "recipients": {"to": [{"address": self.recipient}]},
                    "replyTo": [{"address": reply_to}],
                    "content": {"subject": subject, "plainText": body},
                    "userEngagementTrackingDisabled": True,
                }, operation_id=operation_id, polling=False)
                result = poller.result()
                if not isinstance(result, dict) or result.get("status") not in ("Running", "NotStarted", "Succeeded"):
                    raise Problem(502, "EMAIL_FAILED", "The email service did not accept your request. Your details are still here; please try again later.")
        except Problem:
            raise
        except Exception:
            # Provider exception text can contain request details or credentials.
            raise Problem(502, "EMAIL_UNCONFIRMED", "We could not confirm that your request was accepted. Keep your reference and download your summary before trying again.") from None
