"""Run: python api/dev.py --env .planning/.env. No cloud emulator required."""

import argparse
import mimetypes
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit


def load_env(path):
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if value[:1] in ("'", '"') and value[-1:] == value[:1]:
            value = value[1:-1]
        if key.replace("_", "").isalnum():
            os.environ.setdefault(key, value)


def main():
    parser = argparse.ArgumentParser(description="Rolloff Ready local API and optional production preview")
    parser.add_argument("--env", default=".planning/.env")
    parser.add_argument("--port", type=int, default=7071)
    parser.add_argument("--static", default="", help="Serve a built frontend directory too, e.g. dist")
    args = parser.parse_args()
    load_env(Path(args.env))
    from rr.app import Application
    app = Application()
    static_root = Path(args.static).resolve() if args.static else None

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *values):
            pass  # Do not log URLs, request bodies, addresses, or contact details.

        def respond(self):
            path = urlsplit(self.path).path
            if not path.startswith("/api/") and static_root and self.command in ("GET", "HEAD"):
                candidate = (static_root / unquote(path).lstrip("/")).resolve()
                if not candidate.is_relative_to(static_root):
                    self.send_error(404)
                    return
                if not candidate.is_file():
                    candidate = static_root / "index.html"
                if not candidate.is_file():
                    self.send_error(404)
                    return
                payload = candidate.read_bytes()
                self.send_response(200)
                content_type = mimetypes.guess_type(str(candidate))[0] or "application/octet-stream"
                if candidate.suffix == ".js":
                    content_type = "text/javascript"
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(payload)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self.send_error(400)
                return
            if length < 0 or length > 16384:
                self.send_error(413)
                return
            response = app.handle(self.command, path, dict(self.headers), self.rfile.read(length), self.client_address[0])
            self.send_response(response.status)
            for key, value in response.headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(response.body)))
            self.end_headers()
            self.wfile.write(response.body)

        do_GET = do_POST = do_HEAD = respond

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Rolloff Ready available at http://127.0.0.1:{args.port}; credentials stay on the server.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
