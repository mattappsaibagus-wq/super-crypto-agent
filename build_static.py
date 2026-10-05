#!/usr/bin/env python3
"""Build a static snapshot of the dashboard for Netlify.

Netlify can't run the Flask server, so at build time we render every
data endpoint through Flask's test client into ``public/``. The live
market endpoints (/api/coin, /api/chart, /api/ohlc) are served by the
Netlify Function in ``netlify/functions/market.mjs`` instead.

Scans keep running in GitHub Actions (scan.yml); each scan commits to
main, which triggers a fresh Netlify build with the new data.
"""
from __future__ import annotations

import os
import shutil

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE_DIR, "public")
API_OUT = os.path.join(OUT, "api")

# Import without triggering any cache pre-warming / network calls.
import server  # noqa: E402

JSON_ENDPOINTS = ["status", "report", "signals", "attribution", "agents", "kronos", "portfolio"]


def main() -> None:
    shutil.rmtree(OUT, ignore_errors=True)
    os.makedirs(API_OUT, exist_ok=True)
    client = server.app.test_client()

    html = client.get("/").get_data(as_text=True)
    # The "Run Full Scan" button can't spawn a pipeline on static hosting -
    # scans run on GitHub Actions every 6h and redeploy the site.
    html = html.replace(
        "'Scan complete!'",
        "'Scans run automatically every 6h (GitHub Actions) - showing latest results'",
    )
    with open(os.path.join(OUT, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)

    for name in JSON_ENDPOINTS + ["health"]:
        path = "/health" if name == "health" else f"/api/{name}"
        resp = client.get(path)
        if resp.status_code != 200:
            raise SystemExit(f"{path} returned {resp.status_code}")
        target = os.path.join(OUT if name == "health" else API_OUT, f"{name}.json")
        with open(target, "w", encoding="utf-8") as f:
            f.write(resp.get_data(as_text=True))
        print(f"  wrote {os.path.relpath(target, BASE_DIR)}")

    print(f"Static site built in {os.path.relpath(OUT, BASE_DIR)}/")


if __name__ == "__main__":
    main()
