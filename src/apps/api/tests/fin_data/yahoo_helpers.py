"""Yahoo chart fixtures built in code (no network)."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from tests.fin_data.helpers import Reply

ENV_EXAMPLE = Path(__file__).resolve().parents[4] / "infra" / "docker" / "staging.env.example"


def ts(day: str, tz: str = "Asia/Shanghai", hh: int = 9, mm: int = 30) -> int:
    y, m, d = (int(x) for x in day.split("-"))
    return int(datetime(y, m, d, hh, mm, tzinfo=ZoneInfo(tz)).timestamp())


def chart(rows, *, symbol="600519.SS", currency="CNY", tz="Asia/Shanghai", events=None, adj=True, hh=9, mm=30):
    """rows: (day, open, high, low, close, volume, adjclose) with None allowed in any value."""
    doc = {"chart": {"result": [{"meta": {"currency": currency, "symbol": symbol, "exchangeTimezoneName": tz, "gmtoffset": 28800},
                                 "timestamp": [ts(r[0], tz, hh, mm) for r in rows],
                                 "indicators": {"quote": [{"open": [r[1] for r in rows], "high": [r[2] for r in rows],
                                                           "low": [r[3] for r in rows], "close": [r[4] for r in rows],
                                                           "volume": [r[5] for r in rows]}]}}], "error": None}}
    if adj:
        doc["chart"]["result"][0]["indicators"]["adjclose"] = [{"adjclose": [r[6] for r in rows]}]
    if events:
        doc["chart"]["result"][0]["events"] = events
    return doc


def reply(doc, status=200):
    return Reply(status, json.dumps(doc).encode())


MOUTAI_ROWS = [
    ("2026-11-16", 1500.5, 1520.0, 1495.0, 1510.25, 25000, 1500.0),
    ("2026-11-17", 1510.0, 1530.0, 1505.0, 1525.0, 0, 1515.0),            # zero-volume placeholder: dropped
    ("2026-11-18", None, 1535.0, 1520.0, 1530.0, 20000, 1520.0),          # null open: dropped
    ("2026-11-18", 1525.0, 1535.0, 1520.0, 1530.0, 21000.0, 1520.0),      # duplicate day after the null row is kept (null dropped)
    ("2026-11-19", 1530.0, 1540.0, 1528.0, 1538.0, 18000, None),          # null adjclose: bar kept, no factor
    ("2026-11-20", 1538.0, 1545.0, 1530.0, 1540.0, 5000, 1530.0),         # today, partial: beyond the latest completed session
]
MOUTAI_EVENTS = {"dividends": {str(ts("2026-11-17")): {"amount": 27.6735, "date": ts("2026-11-17")}},
                 "splits": {str(ts("2026-11-18")): {"date": ts("2026-11-18"), "numerator": 2, "denominator": 1, "splitRatio": "2:1"}}}


def personal_dev_policy_json() -> str:
    for line in ENV_EXAMPLE.read_text().splitlines():
        if line.startswith("# FORESIGHTNINE_FIN_DATA_POLICY='") and '"dev-personal-platform-key"' in line:
            return line.split("='", 1)[1].rstrip("'")
    raise AssertionError("personal-dev policy example not found")
