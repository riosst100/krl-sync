"""
Vercel function /api/sync

    GET  (Vercel Cron, or by hand)  start a run (stations: krl-app settings, else KCI_SYNC_STATIONS)
         ?info=1                    effective settings (for the web page)
         ?chain=0&budget=25         manual: return the state after ~25 s instead of continuing
         ?dry=1                     fetch from KCI only, send nothing
         ?types=schedules,trains    what to sync (default KCI_SYNC_TYPES)
         ?stations=THB,SUD          which stations (default KCI_SYNC_STATIONS)
         ?limit_trains=10           only the first N trains (testing)
         ?check=1[&station=THB]     test whether this deployment can reach KCI
    POST {"state": {...}}           continue a run (previous invocation, or the web page with
                                    "chain": false, "budget": N; "abort": true stops it)

Every request needs "Authorization: Bearer <CRON_SECRET>".
"""

import json
import os
import sys
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from krl_sync import app  # noqa: E402
from krl_sync.config import Config  # noqa: E402


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        config = Config.from_env()
        if not self.allowed(config):
            return
        cron = "vercel-cron" in (self.headers.get("User-Agent") or "")
        self.respond(*app.start(config, parse_qs(urlsplit(self.path).query), cron=cron))

    def do_POST(self):
        config = Config.from_env()
        if not self.allowed(config):
            return
        length = int(self.headers.get("Content-Length") or 0)
        self.respond(*app.resume(config, self.rfile.read(length)))

    def allowed(self, config) -> bool:
        if not config.cron_secret:
            self.respond(500, {"error": "CRON_SECRET is not set"})
            return False
        if not app.authorized(config, self.headers.get("Authorization")):
            self.respond(401, {"error": "unauthorized"})
            return False
        return True

    def respond(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, indent=2).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
