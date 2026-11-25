"""Read-only smoke test of the deployed apps (spec 12.1 "Deployment smoke",
acceptance scenarios 13, 16 and 17). Changes nothing anywhere: every call
is a GET/OPTIONS without credentials.

    python -m scripts.smoke_test
    python -m scripts.smoke_test --api https://prepband-backend.onrender.com --web https://prepband.netlify.app

Checks:
  * the API wakes from idle sleep (retries for up to ~90 s) and /health/ready
    reaches the database;
  * CORS allows the real frontend origin and refuses an unknown one;
  * protected and admin routes refuse requests without a token (401);
  * an unknown certificate code gives 404 (no enumeration);
  * the frontend serves deep links (SPA fallback) with its security headers;
  * the built JavaScript contains no server secrets.

Exit code 0 when every check passes. Uses only the standard library.
"""

import argparse
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

DEFAULT_API = "https://prepband-backend.onrender.com"
DEFAULT_WEB = "https://prepband.netlify.app"
USER_AGENT = "prepband-smoke-test/1.0"

# Strings that must never appear in a browser bundle. The Supabase anon key
# is public by design, so only server-side secrets are listed.
SECRET_PATTERNS = {
    "service-role key": re.compile(r"service_role"),
    "database URL": re.compile(r"postgres(?:ql)?(?:\+\w+)?://[^\"'\s]+:[^\"'\s]+@"),
    "R2 secret name": re.compile(r"R2_SECRET_ACCESS_KEY"),
    "Resend key": re.compile(r"\bre_[A-Za-z0-9]{20,}"),
    "private key": re.compile(r"-----BEGIN (?:RSA |EC )?PRIVATE KEY-----"),
}


@dataclass
class Result:
    name: str
    ok: bool
    detail: str = ""


def request(url: str, method: str = "GET", headers: dict | None = None, timeout: float = 30) -> tuple[int, dict, str]:
    req = urllib.request.Request(url, method=method, headers={"User-Agent": USER_AGENT, **(headers or {})})  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed https URLs
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as err:
        return err.code, {k.lower(): v for k, v in err.headers.items()}, err.read().decode("utf-8", "replace")


def wake(api: str) -> Result:
    started = time.monotonic()
    last = ""
    while time.monotonic() - started < 90:
        try:
            status, _h, _b = request(f"{api}/api/v1/health", timeout=20)
            if status == 200:
                return Result("API wakes up", True, f"{time.monotonic() - started:.1f} s")
            last = f"HTTP {status}"
        except Exception as exc:  # noqa: BLE001
            last = type(exc).__name__
        time.sleep(5)
    return Result("API wakes up", False, f"no 200 within 90 s ({last})")


def run(api: str, web: str) -> list[Result]:
    results = [wake(api)]

    status, _h, body = request(f"{api}/api/v1/health/ready")
    results.append(Result("Database reachable", status == 200, f"HTTP {status} {body[:80]}"))

    _s, headers, _b = request(
        f"{api}/api/v1/tests", method="OPTIONS", headers={"Origin": web, "Access-Control-Request-Method": "GET"}
    )
    results.append(Result("CORS allows the frontend", headers.get("access-control-allow-origin") == web, headers.get("access-control-allow-origin", "none")))
    _s, headers, _b = request(
        f"{api}/api/v1/tests", method="OPTIONS", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"}
    )
    results.append(Result("CORS refuses other origins", "access-control-allow-origin" not in headers))

    for path in ("/api/v1/me", "/api/v1/tests", "/api/v1/me/attempts", "/api/v1/admin/tests", "/api/v1/admin/audit"):
        status, _h, _b = request(f"{api}{path}")
        results.append(Result(f"401 without token: {path}", status == 401, f"HTTP {status}"))

    status, _h, _b = request(f"{api}/api/v1/verify/PB-AAAA-BBBB-CCCC-DDDD")
    results.append(Result("Unknown certificate is 404", status == 404, f"HTTP {status}"))

    status, headers, _b = request(f"{api}/api/v1/health")
    results.append(Result("API security headers", headers.get("x-content-type-options") == "nosniff", headers.get("x-content-type-options", "missing")))

    status, headers, html = request(f"{web}/app/practice")
    results.append(Result("Frontend deep link (SPA fallback)", status == 200 and "<div id=\"root\"" in html, f"HTTP {status}"))
    for name in ("x-frame-options", "x-content-type-options", "referrer-policy"):
        results.append(Result(f"Frontend header {name}", name in headers, headers.get(name, "missing")))

    leaks: list[str] = []
    scripts = sorted(set(re.findall(r'src="(/assets/[^"]+\.js)"', html)))
    for script in scripts:
        _s, _h, js = request(f"{web}{script}")
        leaks += [f"{label} in {script}" for label, pattern in SECRET_PATTERNS.items() if pattern.search(js)]
    results.append(Result("No secrets in frontend bundle", bool(scripts) and not leaks, "; ".join(leaks) or f"{len(scripts)} file(s) checked"))
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--api", default=DEFAULT_API)
    parser.add_argument("--web", default=DEFAULT_WEB)
    args = parser.parse_args()
    results = run(args.api.rstrip("/"), args.web.rstrip("/"))
    for r in results:
        print(f"  {'PASS' if r.ok else 'FAIL'}  {r.name}{'  - ' + r.detail if r.detail else ''}")
    failed = sum(1 for r in results if not r.ok)
    print(f"{len(results) - failed}/{len(results)} checks passed.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
