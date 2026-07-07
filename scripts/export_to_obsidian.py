"""Export latest quotes summary into an Obsidian-friendly Markdown file.

Writes to either the user's Obsidian vault (if `OBSIDIAN_VAULT` env var set)
or to `market_data/data/{date}/obsidian.md` so it can be consumed locally.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

TZ_BEIJING = timezone(timedelta(hours=8))
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
CONFIG_DIR = ROOT / "config"


def resolve_date(date_str: Optional[str] = None) -> str:
    if date_str:
        return date_str
    return datetime.now(TZ_BEIJING).strftime("%Y-%m-%d")


def load_holdings() -> list[dict[str, Any]]:
    path = CONFIG_DIR / "holdings.json"
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8")).get("symbols", [])


def read_quote(symbol: str, date_str: str) -> Optional[dict[str, Any]]:
    p = DATA_DIR / date_str / "quotes" / f"{symbol}.json"
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, list) and data:
            first = data[-1]
            return first if isinstance(first, dict) else None
        return None
    except Exception:
        return None


def build_md(date_str: str) -> str:
    holdings = load_holdings()
    lines = [f"# Market snapshot — {date_str}\n"]
    lines.append("| Symbol | Market | Price | Source | Time |")
    lines.append("|---:|:--:|--:|:--:|:--:|")

    for item in holdings:
        sym = item.get("symbol")
        mkt = item.get("market", "")
        q = read_quote(sym, date_str)
        if q:
            price = q.get("close") or q.get("price") or "-"
            src = q.get("source", "-")
            ts = q.get("timestamp", q.get("trade_date", "-"))
            lines.append(f"| {sym} | {mkt} | {price} | {src} | {ts} |")
        else:
            lines.append(f"| {sym} | {mkt} | - | - | - |")

    return "\n".join(lines)


def main(date_str: Optional[str] = None) -> int:
    date_str = resolve_date(date_str)
    md = build_md(date_str)

    # Write to data/{date}/obsidian.md
    out_dir = DATA_DIR / date_str
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "obsidian.md"
    out_path.write_text(md, encoding="utf-8")

    # Optionally write to user's Obsidian vault
    vault = os.environ.get("OBSIDIAN_VAULT")
    if vault:
        try:
            vdir = Path(vault) / "market_data"
            vdir.mkdir(parents=True, exist_ok=True)
            vfile = vdir / f"snapshot-{date_str}.md"
            vfile.write_text(md, encoding="utf-8")
        except Exception:
            pass

    print(f"Wrote obsidian summary to {out_path}")
    if vault:
        print(f"Also wrote to vault: {vfile}")
    return 0


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Export market snapshot to Obsidian")
    parser.add_argument("--date", type=str, default=None, help="Date YYYY-MM-DD")
    args = parser.parse_args()
    raise SystemExit(main(args.date))
