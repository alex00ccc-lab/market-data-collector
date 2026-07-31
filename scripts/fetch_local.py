"""Local market data fetch — one-shot pipeline for Windows scheduled task.

Usage:
    python market_data/scripts/fetch_local.py                    # auto-detect markets by time
    python market_data/scripts/fetch_local.py --markets US,JP    # specific markets
    python market_data/scripts/fetch_local.py --all              # all markets
    python market_data/scripts/fetch_local.py --dry-run          # fetch only, no git push

Runs: sync_holdings → fetch → indicators → git push
Logs to market_data/logs/fetch_local.log
On failure: sends WeChat Work notification.
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ── Paths ────────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent  # market_data/
PROJECT_ROOT = ROOT.parent  # holdings-briefing/
DATA_DIR = ROOT / "data"
LOGS_DIR = ROOT / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)

TZ_BEIJING = timezone(timedelta(hours=8))
NOW = datetime.now(TZ_BEIJING)

# ── Logging ──────────────────────────────────────────────────────────────
logger = logging.getLogger("fetch_local")
logger.setLevel(logging.INFO)

log_file = LOGS_DIR / "fetch_local.log"
fh = logging.FileHandler(log_file, encoding="utf-8")
fh.setLevel(logging.INFO)
fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(fh)

ch = logging.StreamHandler(sys.stdout)
ch.setLevel(logging.INFO)
ch.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(ch)


# ── Helpers ──────────────────────────────────────────────────────────────

def _run(cmd: list[str], cwd: Path = None, timeout: int = 300) -> subprocess.CompletedProcess:
    """Run a command, log output, return result."""
    cwd = cwd or ROOT
    logger.info("Running: %s (cwd=%s)", " ".join(cmd), cwd)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=str(cwd))


def _run_ok(result: subprocess.CompletedProcess) -> bool:
    if result.returncode != 0:
        logger.error("Command failed (exit %d):\nSTDOUT: %s\nSTDERR: %s",
                     result.returncode,
                     result.stdout[-500:] if result.stdout else "(empty)",
                     result.stderr[-500:] if result.stderr else "(empty)")
        return False
    return True


def _send_failure_notification(errors: list[str]):
    """Send a WeChat Work notification about fetch failures."""
    try:
        sys.path.insert(0, str(PROJECT_ROOT / "src"))
        from wecom_notifier import send_briefing
        msg = f"⚠️ 行情抓取异常 ({NOW.strftime('%Y-%m-%d %H:%M')})\n\n"
        for e in errors[:5]:
            msg += f"  • {e}\n"
        if len(errors) > 5:
            msg += f"  ... 还有 {len(errors) - 5} 个错误\n"
        msg += f"\n日志: {log_file}"
        send_briefing(msg)
        logger.info("Failure notification sent to WeChat")
    except Exception as e:
        logger.warning("Failed to send WeChat notification: %s", e)


def _git_status_clean(cwd: Path = None) -> bool:
    """Check if git working tree has uncommitted changes."""
    cwd = cwd or ROOT
    result = subprocess.run(
        ["git", "status", "--porcelain", "--", "data/"],
        capture_output=True, text=True, timeout=10, cwd=str(cwd)
    )
    return not result.stdout.strip()


# ── Market detection ─────────────────────────────────────────────────────

def auto_markets() -> list[str]:
    """Determine which markets to fetch based on current Beijing time."""
    hour = NOW.hour
    if 8 <= hour < 14:
        return ["US", "JP"]   # Morning: US (previous close) + JP
    elif 14 <= hour < 20:
        return ["A", "HK"]    # Afternoon: A + HK after close
    else:
        return ["US", "JP"]   # Default fallback


def markets_from_holdings() -> set[str]:
    """Read holdings.json to see which markets have positions."""
    holdings_path = ROOT / "config" / "holdings.json"
    if not holdings_path.exists():
        return {"US", "A", "HK", "JP"}
    try:
        data = json.loads(holdings_path.read_text(encoding="utf-8"))
        markets = {s.get("market", "US") for s in data.get("symbols", [])}
        return markets
    except Exception:
        return {"US", "A", "HK", "JP"}


# ── Pipeline steps ───────────────────────────────────────────────────────

def step_sync_holdings() -> bool:
    """Sync holdings.xlsx → market_data/config/holdings.json."""
    logger.info("=" * 60)
    logger.info("Step 1/4: Sync holdings")
    result = _run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "sync_holdings_to_marketdata.py")],
        cwd=PROJECT_ROOT,
    )
    return _run_ok(result)


def step_fetch(markets: list[str]) -> tuple[bool, list[str]]:
    """Run fetch.py for specified markets."""
    logger.info("=" * 60)
    logger.info("Step 2/4: Fetch market data (%s)", ", ".join(markets))
    errors = []

    # fetch.py handles ALL symbols internally — call once, not per-market
    result = _run(
        [sys.executable, str(ROOT / "scripts" / "fetch.py"), "--force", "--lenient"],
        cwd=ROOT,
        timeout=600,
    )

    # Parse fetch log for errors
    today_str = NOW.strftime("%Y-%m-%d")
    fetch_log_path = DATA_DIR / today_str / "_fetch_log.json"
    if fetch_log_path.exists():
        try:
            flog = json.loads(fetch_log_path.read_text(encoding="utf-8"))
            errs = flog.get("errors", [])
            if errs:
                errors.extend(errs)
            ok = flog.get("symbols_succeeded", 0)
            total = flog.get("symbols_attempted", 0)
            logger.info("Fetch: %d/%d OK, %d errors", ok, total, len(errs))
            # Log per-source health
            health = flog.get("source_health", {})
            for src, h in health.items():
                logger.info("  source %s: %s (%d ok, %d failed)",
                           src, h.get("success_rate", "?"), h.get("ok", 0), h.get("failed", 0))
        except Exception:
            pass

    if not _run_ok(result):
        errors.append(f"Fetch script failed (exit {result.returncode})")

    return len(errors) == 0, errors


def step_indicators() -> bool:
    """Compute technical indicators for today's data."""
    logger.info("=" * 60)
    logger.info("Step 3/4: Compute indicators")
    today_str = NOW.strftime("%Y-%m-%d")
    result = _run(
        [sys.executable, str(ROOT / "scripts" / "indicators.py"), "--date", today_str],
        cwd=ROOT,
    )
    return _run_ok(result)


def step_git_push() -> bool:
    """Commit and push data changes to market-data-collector repo."""
    logger.info("=" * 60)
    logger.info("Step 4/4: Git commit + push")

    if _git_status_clean(ROOT):
        logger.info("No data changes — skipping git push")
        return True

    today_str = NOW.strftime("%Y-%m-%d")
    # Read fetch log for summary
    n_ok = 0
    n_total = 0
    fetch_log_path = DATA_DIR / today_str / "_fetch_log.json"
    if fetch_log_path.exists():
        try:
            flog = json.loads(fetch_log_path.read_text(encoding="utf-8"))
            n_ok = flog.get("symbols_succeeded", 0)
            n_total = flog.get("symbols_attempted", 0)
        except Exception:
            pass

    msg = f"data: local fetch {today_str} ({n_ok}/{n_total} OK)"

    # Stage
    result = _run(["git", "add", "data/"], cwd=ROOT)
    if not _run_ok(result):
        return False

    # Commit (allow empty in case only fallback skeletons changed)
    result = _run(["git", "commit", "-m", msg, "--allow-empty"], cwd=ROOT)
    if not _run_ok(result):
        return False

    # Push to market-data-collector master
    result = _run(["git", "push", "origin", "master"], cwd=ROOT, timeout=60)
    if not _run_ok(result):
        logger.error("Git push failed — check network / SSH key")
        return False

    logger.info("Pushed to origin/master: %s", msg)
    return True


# ── Main ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Local market data fetch pipeline")
    parser.add_argument("--markets", type=str, default=None,
                        help="Comma-separated markets (US,JP,A,HK). Default: auto-detect by time.")
    parser.add_argument("--all", action="store_true",
                        help="Fetch all markets regardless of time")
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch + indicators only, skip git push")
    args = parser.parse_args()

    if args.all:
        markets = ["US", "JP", "A", "HK"]
    elif args.markets:
        markets = [m.strip() for m in args.markets.split(",")]
    else:
        markets = auto_markets()

    logger.info("╔══════════════════════════════════════════════════════════╗")
    logger.info("║  fetch_local.py — %s", NOW.strftime("%Y-%m-%d %H:%M"))
    logger.info("║  Markets: %s", ", ".join(markets))
    logger.info("║  Dry run: %s", args.dry_run)
    logger.info("╚══════════════════════════════════════════════════════════╝")

    all_errors: list[str] = []
    success = True

    # Step 1: Sync holdings
    if not step_sync_holdings():
        all_errors.append("holdings sync failed")
        success = False

    # Step 2: Fetch
    fetch_ok, fetch_errors = step_fetch(markets)
    all_errors.extend(fetch_errors)
    if not fetch_ok:
        success = False

    # Step 3: Indicators
    if not step_indicators():
        all_errors.append("indicator computation failed")
        success = False

    # Step 4: Git push (skip if dry-run)
    if not args.dry_run:
        if not step_git_push():
            all_errors.append("git push failed")
            success = False
    else:
        logger.info("--dry-run: skipping git push")

    # ── Summary ──
    logger.info("=" * 60)
    if success:
        logger.info("SUCCESS: fetch pipeline complete for %s", ", ".join(markets))
    else:
        logger.error("FAILURE: %d errors in fetch pipeline", len(all_errors))
        _send_failure_notification(all_errors)

    # Write health JSON
    health_path = DATA_DIR / "_health.json"
    health = {
        "last_run": NOW.isoformat(),
        "markets": markets,
        "success": success,
        "errors": all_errors[:10],
    }
    health_path.write_text(json.dumps(health, ensure_ascii=False, indent=2), encoding="utf-8")

    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
