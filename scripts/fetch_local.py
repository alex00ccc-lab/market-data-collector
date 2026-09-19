"""Local market data fetch — one-shot pipeline for Windows scheduled task.

Usage:
    python market_data/scripts/fetch_local.py                    # auto-detect (A/HK)
    python market_data/scripts/fetch_local.py --markets A,HK     # explicit markets
    python market_data/scripts/fetch_local.py --dry-run          # fetch only, no git push

Ownership (P0-D, fail-closed allowlist): local is A/HK only. US/JP/EU are CI's job
(fetch-daily.yml); since v14.41 CI also fetches A/HK (tencent source reachable from
GitHub Actions US runner), so local is a backup path. Any non-A/HK market → reject (exit 1).

Runs: sync_holdings → fetch → indicators → merge_prices → [security gate] → git push
Logs to market_data/logs/fetch_local.log
On failure: sends WeChat Work notification.
"""

from __future__ import annotations

import argparse
import json
import logging
import msvcrt
import os
import subprocess
import sys
import time
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

# ── P0-B deterministic auth (Option B: GCM + hard timeout backstop) ─────
# Keep GCM (PAT stays in Windows Credential Manager, never in a file). Close git's
# terminal-prompt channel via GIT_TERMINAL_PROMPT=0. GCM's own GUI channel has no
# reliable "never prompt" knob in 2.7.3 (credential.interactive=false also breaks
# stored-credential retrieval), so the hard timeout in _run() + kill-tree is the
# fast-fail backstop: on a missing/expired PAT the push fails bounded, not hang.
# POC evidence: plans/p0-data-exfiltration-prevention.md §4 P0-B.
os.environ.setdefault("GIT_TERMINAL_PROMPT", "0")

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

def _acquire_lock() -> bool:
    """Acquire a file lock to prevent concurrent fetch_local.py runs.

    Returns True if lock acquired, False if another instance is already running.
    """
    lock_path = LOGS_DIR / "fetch.lock"
    try:
        lock_fd = open(str(lock_path), "w")
        msvcrt.locking(lock_fd.fileno(), msvcrt.LK_NBLCK, 1)
        # Store fd globally so it stays open for the process lifetime
        _acquire_lock._fd = lock_fd
        return True
    except (IOError, OSError):
        logger.warning("Another fetch_local.py is running (fetch.lock held) — exiting")
        return False


def _kill_tree(pid: int):
    """Kill a process and its descendants (GCM child holds the pipe open)."""
    try:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                       capture_output=True, timeout=10)
    except Exception:
        pass


def _run(cmd: list[str], cwd: Path = None, timeout: int = 300) -> subprocess.CompletedProcess:
    """Run a command with a hard timeout + process-tree kill on expiry.

    Popen+communicate(timeout) so a timed-out command (e.g. git push whose GCM
    child hangs on a GUI prompt) is killed along with its descendants and reported
    as a failure (returncode 124) instead of crashing the pipeline. Callers check
    via _run_ok(), which treats non-zero returncode as failure.
    """
    cwd = cwd or ROOT
    logger.info("Running: %s (cwd=%s)", " ".join(cmd), cwd)
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            cwd=str(cwd), encoding="utf-8", errors="replace",
        )
    except Exception as exc:
        logger.error("Failed to spawn %s: %s", " ".join(cmd), exc)
        return subprocess.CompletedProcess(cmd, 1, "", f"spawn failed: {exc}")

    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
    except subprocess.TimeoutExpired:
        _kill_tree(proc.pid)
        proc.kill()
        stdout, stderr = proc.communicate()
        logger.error("Command timed out after %ds (killed process tree): %s", timeout, " ".join(cmd))
        return subprocess.CompletedProcess(cmd, 124, stdout, f"timed out after {timeout}s")


def _run_ok(result: subprocess.CompletedProcess) -> bool:
    if result.returncode != 0:
        logger.error("Command failed (exit %d):\nSTDOUT: %s\nSTDERR: %s",
                     result.returncode,
                     result.stdout[-500:] if result.stdout else "(empty)",
                     result.stderr[-500:] if result.stderr else "(empty)")
        return False
    return True


# ── P0-C failure classification + retry/backoff ──────────────────────────
RETRY_ATTEMPTS = 4
RETRY_BACKOFF_SECONDS = (30, 60, 120)

_NFF_MARKERS = ("non-fast-forward",)
_AUTH_MARKERS = (
    "could not read username", "authentication failed", "403 forbidden",
    "error: 403", "permission denied", "access denied",
    "unable to get password from user", "terminal prompts disabled",
)
_MERGE_MARKERS = ("automatic merge failed", "conflict", "unmerged paths")
_NETWORK_MARKERS = (
    "failed to connect", "could not resolve host", "connection reset",
    "connection timed out", "connection refused", "port 443",
    "early eof", "rpc failed", "network is unreachable", "unable to access",
)


def classify_git_failure(returncode: int, stdout: str, stderr: str) -> str:
    """Classify a git failure into one of six categories.

    Returns "timeout" | "non_fast_forward" | "auth" | "merge_conflict" |
    "network" | "unknown". Only "network" is retryable; everything else is
    fail-closed (fast-fail, no retry).
    """
    if returncode == 124:
        return "timeout"
    text = ((stdout or "") + "\n" + (stderr or "")).lower()
    if any(m in text for m in _NFF_MARKERS):
        return "non_fast_forward"
    if any(m in text for m in _AUTH_MARKERS):
        return "auth"
    if any(m in text for m in _MERGE_MARKERS):
        return "merge_conflict"
    if any(m in text for m in _NETWORK_MARKERS):
        return "network"
    return "unknown"


def _git_retry(cmd: list[str], cwd: Path = None, timeout: int = 300,
               sleep_fn=time.sleep) -> subprocess.CompletedProcess:
    """Run a git command, retrying transient network failures with backoff.

    Only "network" failures are retried (waits RETRY_BACKOFF_SECONDS).
    auth/timeout/non_fast_forward/merge_conflict/unknown return immediately.
    sleep_fn is injectable so tests can assert the backoff schedule without
    actually sleeping.
    """
    for attempt in range(RETRY_ATTEMPTS):
        result = _run(cmd, cwd=cwd, timeout=timeout)
        if result.returncode == 0:
            return result
        cls = classify_git_failure(result.returncode, result.stdout, result.stderr)
        if cls == "network" and attempt < RETRY_ATTEMPTS - 1:
            wait = RETRY_BACKOFF_SECONDS[attempt]
            logger.warning("Transient network failure (%s) — retry %d/%d in %ds: %s",
                           cls, attempt + 1, RETRY_ATTEMPTS - 1, wait, " ".join(cmd))
            sleep_fn(wait)
            continue
        return result
    # Unreachable (loop always returns), kept for type-checker completeness.
    return subprocess.CompletedProcess(cmd, 1, "", "retry loop exhausted")


def _handle_non_fast_forward(sleep_fn=time.sleep) -> str:
    """Handle a non-fast-forward push rejection: STOP + fetch + ownership check.

    Never auto pull --rebase / -X theirs / --allow-unrelated-histories. Reconciles
    only when local and CI touched disjoint files (mechanical safe recovery path —
    NOT an ownership policy, which is P0-D). Returns "ok" | "push_failed" |
    "ownership_conflict:<files>" | "reconcile_failed:<detail>".
    """
    # 1. Refresh origin/master (retry network).
    fetch = _git_retry(["git", "fetch", "origin"], cwd=ROOT, timeout=60, sleep_fn=sleep_fn)
    if not _run_ok(fetch):
        cls = classify_git_failure(fetch.returncode, fetch.stdout, fetch.stderr)
        return f"reconcile_failed: fetch failed ({cls})"

    # 2. Merge-base. Missing → unrelated histories → STOP (never --allow-unrelated-histories).
    mb = _run(["git", "merge-base", "HEAD", "origin/master"], cwd=ROOT, timeout=30)
    if not _run_ok(mb):
        return "reconcile_failed: no merge-base (unrelated histories)"
    merge_base = mb.stdout.strip()

    # 3. Divergent file sets (local vs remote since the common ancestor).
    local = _run(["git", "diff", "--name-only", merge_base, "HEAD"], cwd=ROOT, timeout=30)
    remote = _run(["git", "diff", "--name-only", merge_base, "origin/master"], cwd=ROOT, timeout=30)
    if not (_run_ok(local) and _run_ok(remote)):
        return "reconcile_failed: could not compute divergent file sets"
    local_only = set(local.stdout.splitlines())
    remote_only = set(remote.stdout.splitlines())

    # 4. Overlap → ownership conflict → manual (P0-6 authority question).
    overlap = local_only & remote_only
    if overlap:
        files = ", ".join(sorted(overlap)[:10])
        logger.error("Ownership conflict: local and CI both wrote %d files (e.g. %s)", len(overlap), files)
        return f"ownership_conflict: {files}"

    # 5. Disjoint → eligible for explicit merge (merge itself still verified).
    merge = _run(["git", "merge", "origin/master", "--no-edit"], cwd=ROOT, timeout=120)
    if not _run_ok(merge):
        return "reconcile_failed: merge failed"

    # 6. Retry push.
    push = _git_retry(["git", "push", "origin", "master"], cwd=ROOT, timeout=60, sleep_fn=sleep_fn)
    if not _run_ok(push):
        cls = classify_git_failure(push.returncode, push.stdout, push.stderr)
        return f"push_failed: after reconcile ({cls})"
    return "ok"


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

# P0-D ownership gate: local fetches A/HK only (fail-closed allowlist).
# US/JP/EU — and A/HK as of v14.41 — are CI's job (fetch-daily.yml --markets US,JP,EU,A,HK).
ALLOWED_LOCAL_MARKETS = {"A", "HK"}


def auto_markets() -> list[str]:
    """Default markets for manual runs without --markets.

    Local = A + HK only (backup path). All markets (US/JP/EU/A/HK) are
    fetched by GitHub Actions CI (fetch-daily.yml) since v14.41. The
    ALLOWED_LOCAL_MARKETS gate in main() rejects any non-A/HK market.
    """
    return ["A", "HK"]


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


def step_fetch(markets: list[str], force: bool = False) -> tuple[bool, list[str]]:
    """Run fetch.py for specified markets.

    Calendar awareness: TradingCalendar.should_fetch() in fetch.py decides
    per-symbol whether to attempt fetch.  On weekends/holidays, symbols are
    skipped rather than reported as failures.
    """
    logger.info("=" * 60)
    logger.info("Step 2/4: Fetch market data (%s)", ", ".join(markets))
    errors = []

    # Build fetch.py args — restrict to requested markets so local only fetches
    # A/HK (US/JP/EU are CI's job via fetch-daily.yml). Only pass --force when requested.
    fetch_args = [sys.executable, str(ROOT / "scripts" / "fetch.py"), "--lenient"]
    if markets:
        fetch_args += ["--markets", ",".join(markets)]
    if force:
        fetch_args.append("--force")

    result = _run(fetch_args, cwd=ROOT, timeout=600)

    # Parse fetch log for errors
    today_str = NOW.strftime("%Y-%m-%d")
    fetch_log_path = DATA_DIR / today_str / "_fetch_log.json"
    skipped_count = 0
    stale_count = 0
    if fetch_log_path.exists():
        try:
            flog = json.loads(fetch_log_path.read_text(encoding="utf-8"))
            errs = flog.get("errors", [])
            skipped_list = flog.get("skipped", [])
            skipped_count = len(skipped_list)
            if errs:
                errors.extend(errs)
            ok = flog.get("symbols_succeeded", 0)
            total = flog.get("symbols_attempted", 0)
            logger.info("Fetch: %d/%d OK, %d errors, %d skipped (market closed)",
                       ok, total, len(errs), skipped_count)
            # Log per-source health
            health = flog.get("source_health", {})
            for src, h in health.items():
                stale = h.get("stale", 0)
                if stale:
                    stale_count += stale
                    logger.info("  source %s: %s (%d ok, %d failed, %d stale)",
                               src, h.get("success_rate", "?"), h.get("ok", 0), h.get("failed", 0), stale)
                else:
                    logger.info("  source %s: %s (%d ok, %d failed)",
                               src, h.get("success_rate", "?"), h.get("ok", 0), h.get("failed", 0))
        except Exception:
            pass

    # If all symbols were skipped (market closed), that's not an error
    if not errors and skipped_count > 0 and not _run_ok(result):
        # fetch.py returned non-zero because lenient didn't help,
        # but there were no real errors — only skips
        logger.info("All %d symbols skipped (markets closed) — no errors", skipped_count)
        return True, []

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


def step_merge_prices() -> bool:
    """Merge fetched quotes into the main repo cache/prices (offline, no API).

    Keeps the local accumulated price library fresh (holdings-briefing CLAUDE.md
    §5.5) so the reminder engine / reports read current closes instead of the
    last manual merge. Reads the watchlist synced in Step 1 and the quotes
    fetched in Step 2 — no external API calls. Idempotent: only new dates added.
    """
    logger.info("=" * 60)
    logger.info("Step 3.5/4: Merge into cache/prices")
    result = _run(
        [sys.executable,
         str(PROJECT_ROOT / "src" / "report" / "data_collector.py"),
         "--merge-local", "--all"],
        cwd=PROJECT_ROOT,
        timeout=300,
    )
    return _run_ok(result)


def step_git_push() -> str:
    """Commit and push data changes to market-data-collector repo.

    Returns a status string: "ok" | "no_changes" | "stage_failed" |
    "commit_failed" | "gate_blocked:<detail>" | "push_failed" |
    "ownership_conflict:<files>" | "reconcile_failed:<detail>".
    """
    logger.info("=" * 60)
    logger.info("Step 4/4: Git commit + push")

    if _git_status_clean(ROOT):
        logger.info("No data changes — skipping git push")
        return "no_changes"

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

    # Stage (force-add because data/ is gitignored — CI uses -f as well)
    result = _run(["git", "add", "-f", "data/"], cwd=ROOT)
    if not _run_ok(result):
        return "stage_failed"

    # Commit (allow empty in case only fallback skeletons changed)
    result = _run(["git", "commit", "-m", msg, "--allow-empty"], cwd=ROOT)
    if not _run_ok(result):
        return "commit_failed"

    # P0-A privacy/secret gate (fail-closed) — scan staged + unpublished range BEFORE push.
    try:
        sys.path.insert(0, str(PROJECT_ROOT / "src"))
        from push_security_gate import run_gate
        gate = run_gate(repo=ROOT, allow=["data/**/*.json"])
    except Exception as exc:
        logger.error("Security gate FAILED to run (fail-closed → block push): %s", exc)
        return f"gate_blocked: gate error ({exc})"
    if not gate.passed:
        for v in gate.violations[:10]:
            logger.error("Gate violation: %s", v)
        return "gate_blocked: " + " | ".join(gate.violations[:3])

    # Push to market-data-collector master (P0-C: retry network; NFF → ownership reconcile)
    result = _git_retry(["git", "push", "origin", "master"], cwd=ROOT, timeout=60)
    if not _run_ok(result):
        cls = classify_git_failure(result.returncode, result.stdout, result.stderr)
        if cls == "non_fast_forward":
            logger.warning("Push rejected (non-fast-forward) — running ownership reconcile")
            return _handle_non_fast_forward()
        logger.error("Git push failed (%s) — check network / credential", cls)
        return "push_failed"

    logger.info("Pushed to origin/master: %s", msg)
    return "ok"


def _write_health(markets: list[str], success: bool, errors: list[str]):
    """Write _health.json with the current pipeline status."""
    health_path = DATA_DIR / "_health.json"
    health = {
        "last_run": NOW.isoformat(),
        "markets": markets,
        "success": success,
        "errors": errors[:10],
    }
    health_path.write_text(json.dumps(health, ensure_ascii=False, indent=2), encoding="utf-8")


# ── Main ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Local market data fetch pipeline")
    parser.add_argument("--markets", type=str, default=None,
                        help="Comma-separated markets (A,HK only). Default: auto-detect (A/HK).")
    parser.add_argument("--force", action="store_true",
                        help="Force fetch even outside trading hours / on non-trading days")
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch + indicators only, skip git push")
    args = parser.parse_args()

    # ── File lock: prevent concurrent runs ──
    if not _acquire_lock():
        logger.info("Exiting — another fetch_local.py instance is already running")
        return 0

    if args.markets:
        markets = [m.strip() for m in args.markets.split(",") if m.strip()]
    else:
        markets = auto_markets()

    # P0-D ownership gate (fail-closed): reject any non-A/HK market.
    illegal = [m for m in markets if m not in ALLOWED_LOCAL_MARKETS]
    if illegal:
        logger.error(
            "Rejected markets %s — local fetches A/HK only (US/JP/EU are CI's job). Exiting.",
            illegal,
        )
        return 1

    # ── Calendar check ──
    from utils import TradingCalendar
    cal = TradingCalendar()
    today = NOW.date()
    weekday_name = ["一","二","三","四","五","六","日"][today.weekday()]
    open_markets = [m for m in markets if cal.should_fetch(m, today) or args.force]
    closed_markets = [m for m in markets if m not in open_markets]

    logger.info("╔══════════════════════════════════════════════════════════╗")
    logger.info("║  fetch_local.py — %s (周%s)", NOW.strftime("%Y-%m-%d %H:%M"), weekday_name)
    logger.info("║  Markets: %s", ", ".join(markets))
    if closed_markets and not args.force:
        logger.info("║  Closed (will skip): %s", ", ".join(closed_markets))
    logger.info("║  Force: %s | Dry run: %s", args.force, args.dry_run)
    logger.info("╚══════════════════════════════════════════════════════════╝")

    # If no markets are open and not forcing, exit cleanly
    if not open_markets and not args.force:
        logger.info("All markets closed on %s — nothing to fetch, exiting cleanly", today)
        return 0

    all_errors: list[str] = []
    success = True

    # Step 0: Git pull — sync latest CI data before adding local A-share data.
    # P0-C: pull failure is FATAL (fail-closed) — if we can't confirm we're on the
    # latest remote, we must not produce new local commits (that's the 2026-09-18
    # chain: pull fail → fetch stale → commit → push non-fast-forward).
    if not args.dry_run:
        logger.info("=" * 60)
        logger.info("Step 0/4: Git pull (sync CI data)")
        result = _git_retry(["git", "pull", "origin", "master"], cwd=ROOT, timeout=60)
        if _run_ok(result):
            logger.info("CI data synced")
        else:
            cls = classify_git_failure(result.returncode, result.stdout, result.stderr)
            logger.error("Git pull FAILED (%s) — aborting (won't fetch without CI sync)", cls)
            all_errors.append(f"git pull failed ({cls})")
            _send_failure_notification(all_errors)
            _write_health(markets, False, all_errors)
            return 1

    # Step 1: Sync holdings
    if not step_sync_holdings():
        all_errors.append("holdings sync failed")
        success = False

    # Step 2: Fetch (calendar-aware — fetch.py skips closed-market symbols)
    fetch_ok, fetch_errors = step_fetch(markets, force=args.force)
    all_errors.extend(fetch_errors)
    if not fetch_ok:
        success = False

    # Step 3: Indicators
    if not step_indicators():
        all_errors.append("indicator computation failed")
        success = False

    # Step 3.5: Merge fetched quotes into cache/prices (offline — keep local price lib fresh)
    if not step_merge_prices():
        all_errors.append("cache/prices merge failed")
        success = False

    # Step 4: Git push (skip if dry-run)
    if not args.dry_run:
        push_status = step_git_push()
        if push_status not in ("ok", "no_changes"):
            all_errors.append(push_status)
            success = False
    else:
        logger.info("--dry-run: skipping git push")

    # ── Summary ──
    logger.info("=" * 60)
    if success:
        logger.info("SUCCESS: fetch pipeline complete for %s", ", ".join(markets))
    else:
        # Only send WeChat notification for genuine errors (not market-closed skips)
        real_errors = [e for e in all_errors if "market closed" not in e.lower() and "skipped" not in e.lower()]
        if real_errors:
            logger.error("FAILURE: %d errors in fetch pipeline", len(all_errors))
            _send_failure_notification(all_errors)
        else:
            logger.info("No real errors — all failures were market-closed skips, suppressing notification")

    # Write health JSON
    _write_health(markets, success, all_errors)

    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
