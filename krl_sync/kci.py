"""
GETs JSON from kci.id. A port of krl-app's KciUrlClient plus the kci-fetch
sidecar in one:

- Cloudflare in front of kci.id blocks clients by their TLS/HTTP2 fingerprint,
  so requests go through curl_cffi with a browser fingerprint (KCI_IMPERSONATE,
  then the chrome -> safari -> firefox fallbacks on a block).
- Requests are paced like someone browsing: one at a time, a random wait
  between them (KCI_REQUEST_DELAY_MS, default 3-8 s) and every 20-40 requests
  a random 30-90 s break (KCI_BREAK_EVERY, KCI_BREAK_SECONDS).
- The KCI web API is rate limited (x-ratelimit-limit: 60): HTTP 429 is retried
  after Retry-After (max 2 retries), batches pause when X-RateLimit-Remaining
  gets low.
- A block (Cloudflare page, or 429 that does not clear) raises KciBlocked: the
  caller stops the whole run instead of hammering the block.
"""

import json
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .config import as_range

from curl_cffi import requests
from curl_cffi.requests.exceptions import RequestException

FALLBACK_PROFILES = ["chrome", "safari", "firefox"]
MAX_RATE_LIMIT_RETRIES = 2


class KciError(Exception):
    """One request failed (HTTP error, invalid JSON, unexpected shape)."""


class KciBlocked(KciError):
    """KCI refuses this client: every further request would fail too."""


def is_blocked(response) -> bool:
    """A Cloudflare challenge/block page instead of the API response."""
    if response.headers.get("cf-mitigated"):
        return True

    if response.status_code not in (403, 503):
        return False

    # A 403/503 from the API itself is JSON; Cloudflare answers with HTML
    # ("Attention Required" / "Just a moment").
    return "json" not in response.headers.get("content-type", "")


class Pacer:
    """
    Spaces requests out at random, like a person clicking through timetables.
    A long break is skipped when it would not end before `not_after` (a
    monotonic time), so a Vercel invocation still hands over in time.
    """

    def __init__(self, config, log=print, sleep=time.sleep, clock=time.monotonic, rng=None):
        self.delay = as_range(config.request_delay_ms)
        self.every = as_range(config.break_every)
        self.seconds = as_range(config.break_seconds)
        self.log = log
        self.sleep = sleep
        self.clock = clock
        self.rng = rng or random.Random()
        self.not_after = None
        self.last = None
        self.lock = threading.Lock()
        self.until_break = self.next_break()

    def next_break(self) -> int:
        return self.rng.randint(*self.every) if self.every[1] > 0 and self.seconds[1] > 0 else -1

    def wait(self) -> None:
        """Called before every request; the lock keeps requests one after another."""
        with self.lock:
            if self.last is not None:
                wait = self.rng.uniform(*self.delay) / 1000

                if self.until_break == 0:
                    pause = self.rng.uniform(*self.seconds)
                    if self.not_after is None or self.clock() + pause < self.not_after:
                        self.log(f"pause {pause:.0f}s")
                        wait = max(wait, pause)
                        self.until_break = self.next_break()

                wait -= self.clock() - self.last
                if wait > 0:
                    self.sleep(wait)

            if self.until_break > 0:
                self.until_break -= 1

    def done(self) -> None:
        self.last = self.clock()


class KciClient:
    def __init__(self, config, log=print, sleep=time.sleep, pacer=None):
        self.config = config
        self.log = log
        self.sleep = sleep
        self.pacer = pacer or Pacer(config, log, sleep)
        self.requests = 0

    def profiles(self) -> list[str]:
        first = self.config.impersonate
        return [first] + [p for p in FALLBACK_PROFILES if p != first]

    def get(self, url: str, token: str = ""):
        """
        One GET with a browser fingerprint; the next profile on a Cloudflare block.

        Raises KciBlocked when every profile is blocked, KciError when KCI cannot be reached.
        """
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        blocked = None
        for profile in self.profiles():
            self.pacer.wait()
            started = time.monotonic()
            try:
                response = requests.get(url, headers=headers, impersonate=profile, timeout=self.config.timeout, allow_redirects=False)
            except RequestException as e:
                self.log(f"KCI unreachable: {url}: {e}")
                raise KciError(f"cannot connect to {url}: {e}") from e
            finally:
                self.requests += 1
                self.pacer.done()

            cf_ray = response.headers.get("cf-ray") or "-"
            if not is_blocked(response):
                self.log(f"KCI {response.status_code} {url} ({time.monotonic() - started:.1f}s)")
                return response

            blocked = f"HTTP {response.status_code} from {url} (blocked by Cloudflare, impersonate={profile}, cf-ray {cf_ray})"
            self.log(f"KCI blocked: {blocked}")

        raise KciBlocked(blocked)

    def fetch(self, url: str, token: str = ""):
        """Decoded JSON of one URL; HTTP 429 is waited out and retried."""
        for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
            response = self.get(url, token)
            if response.status_code != 429:
                return self.decode(response, url)
            if attempt < MAX_RATE_LIMIT_RETRIES:
                self.wait_for_rate_limit(response)

        raise KciBlocked(f"HTTP 429 from {url} (rate limited by the provider: too many requests, try again later)")

    def fetch_many(self, urls: dict, token: str = "", concurrency: int = 2) -> dict:
        """
        Fetches many URLs with at most `concurrency` requests in flight. Each
        result is the decoded JSON or the KciError for that URL; a KciBlocked
        is raised.
        """
        results = {}
        keys = list(urls)

        # The pacer spaces the requests out (see Pacer).
        for i in range(0, len(keys), concurrency):
            batch = keys[i:i + concurrency]

            with ThreadPoolExecutor(max_workers=len(batch)) as pool:
                futures = {key: pool.submit(self.get, urls[key], token) for key in batch}
                outcomes = {}
                for key, future in futures.items():
                    try:
                        outcomes[key] = future.result()
                    except KciError as e:
                        outcomes[key] = e

            remaining, limited = None, None
            for key in batch:
                response = outcomes[key]
                if isinstance(response, KciBlocked):
                    raise response
                if isinstance(response, KciError):
                    results[key] = response
                    continue
                if response.status_code == 429:
                    limited = response
                header = response.headers.get("x-ratelimit-remaining")
                if header and header.isdigit():
                    remaining = int(header) if remaining is None else min(remaining, int(header))
                try:
                    results[key] = self.decode(response, urls[key])
                except KciError as e:
                    results[key] = e

            if limited is not None:
                # The caller retries these one by one; give the quota time to refill first.
                self.wait_for_rate_limit(limited)
            elif remaining is not None and remaining < concurrency:
                self.log(f"KCI rate limit almost reached (remaining {remaining}), pausing {self.config.rate_limit_pause_seconds}s")
                self.sleep(self.config.rate_limit_pause_seconds)

        return results

    def wait_for_rate_limit(self, response) -> None:
        retry_after = response.headers.get("retry-after", "")
        seconds = int(retry_after) if retry_after.isdigit() else self.config.rate_limit_pause_seconds
        seconds = max(1, min(seconds, 60))
        self.log(f"KCI rate limited (HTTP 429), waiting {seconds}s")
        self.sleep(seconds)

    @staticmethod
    def decode(response, url: str):
        if response.status_code >= 400:
            hint = " (rate limited by the provider: too many requests, try again in a minute)" if response.status_code == 429 else ""
            raise KciError(f"HTTP {response.status_code} from {url}{hint}")

        try:
            payload = json.loads(response.content)
        except ValueError:
            content_type = response.headers.get("content-type") or "unknown content type"
            raise KciError(f"{url} did not return JSON ({content_type})") from None

        if not isinstance(payload, (dict, list)):
            raise KciError(f"{url} did not return a JSON object or array")

        return payload
