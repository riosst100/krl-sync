"""
Local stand-in for Vercel: serves index.html at / and the /api/sync function,
so the project runs in Docker without the Vercel CLI. No time limit applies
here, but the chained hand-off still works through SELF_URL.
"""

import os
import sys
from http.server import ThreadingHTTPServer
from urllib.parse import urlsplit

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from api.sync import handler as SyncHandler  # noqa: E402

# NO_AUTH=true: no CRON_SECRET needed and the page opens without the login
# form. Only for a port bound to localhost; Vercel keeps the secret.
NO_AUTH = os.environ.get("NO_AUTH", "").lower() in ("1", "true", "yes")


class Handler(SyncHandler):
    def do_GET(self):
        path = urlsplit(self.path).path
        if path in ("/", "/index.html"):
            return self.serve_index()
        if path.rstrip("/") == "/api/sync":
            return super().do_GET()
        self.respond(404, {"error": "not found"})

    def do_POST(self):
        if urlsplit(self.path).path.rstrip("/") == "/api/sync":
            return super().do_POST()
        self.respond(404, {"error": "not found"})

    def allowed(self, config) -> bool:
        return True if NO_AUTH else super().allowed(config)

    def serve_index(self):
        with open(os.path.join(ROOT, "index.html"), "rb") as f:
            body = f.read()
        if NO_AUTH:
            body = body.replace(b"if (stored()) login(stored());", b"login(\"\");")
            body = body.replace(b'$("logout").classList.remove("hidden");', b"")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    print(f"[krl-sync] listening on :{port}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
