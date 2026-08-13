"""Market-label validator — backstop against 6981.T-class mislabeling.

Cross-checks every symbol in ``market_data/config/holdings.json`` against a
canonical exchange-suffix → market map.  A symbol that *carries* an exchange
suffix is unambiguous about which market it belongs to; if the ``market``
field disagrees, the label is wrong and the valuation / fetch routing that
depends on it is corrupt.

Exit codes:
    0 — all symbols consistent (or no symbols to check)
    1 — at least one mislabel / unknown-market error

Usage:
    python scripts/market_validate.py
    python scripts/market_validate.py --holdings <path-to-holdings.json>
    python scripts/market_validate.py --allow-bare    # also flag bare US/JP/EU tickers

This is a standalone script (no third-party deps beyond the stdlib) so it can
run inside the market_data CI workflow and the local pre-push hook alike.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Canonical exchange suffix → market.  Order matters: longest / most specific
# first.  These are the only suffixes this project uses (see CLAUDE.md §6.4).
SUFFIX_MARKET = [
    (".HK", "HK"),
    (".T", "JP"),
    (".SH", "A"),
    (".SZ", "A"),
    # European exchanges — all map to the single "EU" market code (D2).
    (".ST", "EU"),  # Stockholm
    (".DE", "EU"),  # Frankfurt / Xetra
    (".PA", "EU"),  # Paris
    (".AS", "EU"),  # Amsterdam
    (".MI", "EU"),  # Milan
    (".MC", "EU"),  # Madrid
    (".CO", "EU"),  # Copenhagen
    (".OL", "EU"),  # Oslo
    (".HE", "EU"),  # Helsinki
    (".SW", "EU"),  # Switzerland (SIX)
    (".L", "EU"),   # London
    (".VI", "EU"),  # Vienna
    (".LS", "EU"),  # Lisbon
    (".BR", "EU"),  # Brussels
]

VALID_MARKETS = {"A", "HK", "US", "JP", "EU"}

# Bare 6-digit numeric codes are A-shares by project convention.
A_SHARE_NUMERIC = 6


def implied_market(symbol: str) -> str | None:
    """Return the market implied by a symbol's exchange suffix, or None.

    Bare symbols (no recognized suffix) return None — they carry no
    self-describing signal, so we do not guess (avoids false positives).
    """
    sym = symbol.strip().upper()
    for suffix, market in SUFFIX_MARKET:
        if sym.endswith(suffix):
            return market
    return None


def default_holdings_path() -> Path:
    """Resolve holdings.json regardless of where this script lives.

    Layouts supported:
      holdings-briefing/scripts/market_validate.py  → ../market_data/config/holdings.json
      market_data/scripts/market_validate.py        → ../config/holdings.json  (vendored copy)
    """
    parent = Path(__file__).resolve().parent.parent
    cand_briefing = parent / "market_data" / "config" / "holdings.json"
    if cand_briefing.exists():
        return cand_briefing
    cand_marketdata = parent / "config" / "holdings.json"
    if cand_marketdata.exists():
        return cand_marketdata
    return cand_briefing


def load_holdings(path: Path) -> list[dict]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    symbols = raw.get("symbols", [])
    if not isinstance(symbols, list):
        raise ValueError(f"{path}: 'symbols' is not a list")
    return symbols


def validate(symbols: list[dict], allow_bare: bool = False) -> list[str]:
    """Return a list of human-readable error strings (empty = all good)."""
    errors: list[str] = []

    for i, item in enumerate(symbols):
        symbol = str(item.get("symbol", "")).strip()
        market = str(item.get("market", "")).strip().upper()

        if not symbol:
            errors.append(f"entry {i}: empty symbol")
            continue

        # 1. Market code must be one of the known set.
        if market not in VALID_MARKETS:
            errors.append(
                f"{symbol}: unknown market '{item.get('market')!r}' "
                f"(expected one of {sorted(VALID_MARKETS)})"
            )
            continue

        # 2. Suffix vs market field — the core check.
        implied = implied_market(symbol)
        if implied is not None and implied != market:
            errors.append(
                f"{symbol}: suffix implies market '{implied}' "
                f"but field is '{market}'"
            )

        # 3. Optional: bare-symbol heuristics (off by default).
        if allow_bare and implied is None:
            if symbol.isdigit() and len(symbol) == A_SHARE_NUMERIC and market != "A":
                errors.append(
                    f"{symbol}: 6-digit numeric code implies 'A' but field is '{market}'"
                )

    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate market labels in holdings.json")
    parser.add_argument(
        "--holdings",
        type=Path,
        default=default_holdings_path(),
        help="path to holdings.json (default: auto-resolved)",
    )
    parser.add_argument(
        "--allow-bare",
        action="store_true",
        help="also apply bare-symbol heuristics (6-digit → A)",
    )
    args = parser.parse_args(argv)

    if not args.holdings.exists():
        print(f"✗ holdings file not found: {args.holdings}")
        return 1

    try:
        symbols = load_holdings(args.holdings)
    except Exception as e:
        print(f"✗ failed to load {args.holdings}: {e}")
        return 1

    if not symbols:
        print("✓ no symbols to validate")
        return 0

    errors = validate(symbols, allow_bare=args.allow_bare)

    if errors:
        print(f"✗ {len(errors)} market-label error(s) in {args.holdings.name}:")
        for e in errors:
            print(f"  - {e}")
        print("  Fix the market/currency labels, then re-run.")
        return 1

    print(f"✓ {len(symbols)} symbols — market labels consistent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
