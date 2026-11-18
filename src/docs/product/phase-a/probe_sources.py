"""Read-only availability probe; records metadata, never stores source text."""
import hashlib
import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

SOURCES = {
    "msft_ar23": "https://www.microsoft.com/investor/reports/ar23/",
    "msft_ar24": "https://www.microsoft.com/investor/reports/ar24/",
    "apple_fy24": "https://www.apple.com/newsroom/2024/10/apple-reports-fourth-quarter-results/",
    "fred_gdp": "https://fred.stlouisfed.org/series/GDP",
}


def probe(key, url):
    start = time.monotonic()
    item = {"id": key, "url": url, "retrieved_at_utc": datetime.now(timezone.utc).isoformat()}
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; research-availability-probe/1.0)"})
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read(4_000_001)
            item.update(status=response.status, final_url=response.geturl(), content_type=response.headers.get("Content-Type"),
                        byte_count=len(body), sha256=hashlib.sha256(body).hexdigest(), truncated=len(body) > 4_000_000)
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        item.update(status="failed", error_type=type(error).__name__, detail=str(error)[:200])
    item["elapsed_seconds"] = round(time.monotonic() - start, 3)
    return item


if __name__ == "__main__":
    results = [probe(key, url) for key, url in SOURCES.items()]
    target = Path(__file__).with_name("source-probe-2026-11-11.json")
    with target.open("x") as stream:
        stream.write(json.dumps({"method": "single GET, 20 second timeout, no retries; response text not saved", "results": results}, indent=2) + "\n")
    print(json.dumps(results, indent=2))
