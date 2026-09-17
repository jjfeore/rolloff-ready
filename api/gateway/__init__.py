"""Azure Functions transport adapter. Domain logic also runs without Azure locally."""

from functools import lru_cache
from urllib.parse import urlsplit

import azure.functions as func

from rr.app import Application


@lru_cache(maxsize=1)
def application():
    return Application()


def main(req: func.HttpRequest) -> func.HttpResponse:
    # Treat the host forwarding identity as a best-effort limiter key only.
    # A global process limit also applies; neither is a distributed abuse control.
    client = req.headers.get("x-forwarded-for", "unknown").split(",")[-1].strip()
    response = application().handle(req.method, urlsplit(req.url).path, dict(req.headers), req.get_body(), client)
    return func.HttpResponse(body=response.body, status_code=response.status, headers=response.headers)
