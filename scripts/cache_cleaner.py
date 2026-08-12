"""Cache cleaner — TTL-based expiry with whitelist protection.

Whitelist directories are NEVER touched. Expiry directories get files
older than their TTL deleted. Designed for scheduled CI runs.

Usage:
    python scripts/cache_cleaner.py --dry-run          # Preview what would be deleted
    python scripts/cache_cleaner.py                    # Execute cleanup
    python scripts/cache_cleaner.py --verbose          # Detailed output
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("cache_cleaner")

# ============================================================================
# Configuration
# ============================================================================

# Directories that are NEVER cleaned (absolute or relative to project root)
WHITELIST_DIRS: list[str] = [
    "market_data/",           # raw market data source
    "market_data/data/",      # daily quote/indicator data
    "knowledge/frameworks/",  # git submodule — trading-frameworks
    "config/",                # portfolio config and keys
    "reports/",               # generated reports (manual archive, not auto-cleaned)
    ".git/",                  # git internals
    ".github/",               # CI workflows
]

# Directories subject to TTL-based cleaning (relative to project root)
# For directories containing mixed content, only matching files are cleaned
EXPIRY_RULES: dict[str, int] = {
    "cache/frame_score/":     3 * 86400,   # 3 days — frame scoring cache
    "cache/":                 7 * 86400,   # 7 days — fundamentals JSON (excl. manifest + subdirs)
    "cache/vega_libs/":      14 * 86400,   # 14 days — volatility surface libs
}

# Subdirectories within cache/ that are NEVER expired
CACHE_WHITELIST_SUBDIRS: list[str] = [
    "cache/prices/",
    "cache/snapshots/",
    "cache/archive/",
]


# ============================================================================
# Core logic
# ============================================================================

def _get_project_root() -> Path:
    """Resolve project root (holdings-briefing/, two levels up from this script)."""
    # This script is at market_data/scripts/cache_cleaner.py
    # .parent → scripts/ → .parent → market_data/ → .parent → holdings-briefing/
    return Path(__file__).resolve().parent.parent.parent


def _is_whitelisted(rel_path: str) -> bool:
    """Check if a path is in the global whitelist."""
    normalized = rel_path.replace("\\", "/")
    if not normalized.endswith("/"):
        normalized += "/"
    for wl in WHITELIST_DIRS + CACHE_WHITELIST_SUBDIRS:
        wl_norm = wl.replace("\\", "/")
        if not wl_norm.endswith("/"):
            wl_norm += "/"
        if normalized.startswith(wl_norm):
            return True
    return False


def _get_file_age_seconds(filepath: Path) -> float:
    """Age of file in seconds (based on mtime). Returns 0 for unreadable files."""
    try:
        mtime = filepath.stat().st_mtime
        return time.time() - mtime
    except OSError:
        return 0.0


def _should_expire(filepath: Path, ttl_seconds: int) -> bool:
    """Check if file is older than TTL."""
    age = _get_file_age_seconds(filepath)
    return age > ttl_seconds


def run_cleanup(project_root: Path, dry_run: bool = True, verbose: bool = False) -> dict:
    """Run cache cleanup.

    Args:
        project_root: Project root directory.
        dry_run: If True, only report what would be deleted.
        verbose: If True, log each file decision.

    Returns:
        {"deleted": N, "freed_bytes": B, "errors": [...]}
    """
    deleted = 0
    freed_bytes = 0
    errors: list[str] = []

    for exp_dir_rel, ttl in EXPIRY_RULES.items():
        exp_dir = project_root / exp_dir_rel
        if not exp_dir.exists():
            if verbose:
                logger.info("Directory does not exist, skipping: %s", exp_dir_rel)
            continue

        # For the flat cache/ root, only clean top-level JSON files
        # (prices/, snapshots/, archive/, vega_libs/ are whitelisted subdirs)
        if exp_dir_rel.rstrip("/") == "cache":
            files = list(exp_dir.glob("*.json"))
        else:
            files = list(exp_dir.rglob("*"))

        for filepath in files:
            if not filepath.is_file():
                continue

            # Skip .gitkeep and manifest files
            if filepath.name in (".gitkeep", "manifest.json"):
                continue

            rel = str(filepath.relative_to(project_root))

            # Whitelist check
            if _is_whitelisted(rel):
                if verbose:
                    logger.info("WHITELISTED: %s", rel)
                continue

            # TTL check
            if not _should_expire(filepath, ttl):
                if verbose:
                    age_h = _get_file_age_seconds(filepath) / 3600
                    logger.info("KEEP (%.1fh < %dh TTL): %s", age_h, ttl / 3600, rel)
                continue

            # Expire
            try:
                size = filepath.stat().st_size
                if dry_run:
                    logger.info("WOULD DELETE: %s (%d bytes, TTL %dh)",
                                rel, size, ttl // 3600)
                else:
                    filepath.unlink()
                    logger.info("DELETED: %s (%d bytes)", rel, size)
                deleted += 1
                freed_bytes += size
            except OSError as e:
                msg = f"Failed to {'delete' if not dry_run else 'access'} {rel}: {e}"
                errors.append(msg)
                logger.warning(msg)

        # Remove empty directories after cleanup (non-dry-run only)
        if not dry_run and exp_dir.exists():
            for subdir in sorted(exp_dir.rglob("*"), reverse=True):
                if subdir.is_dir() and not any(subdir.iterdir()):
                    try:
                        subdir.rmdir()
                        if verbose:
                            logger.info("Removed empty dir: %s",
                                        str(subdir.relative_to(project_root)))
                    except OSError:
                        pass  # non-empty or permission denied

    return {"deleted": deleted, "freed_bytes": freed_bytes, "errors": errors}


# ============================================================================
# Summary / audit
# ============================================================================

def print_summary(result: dict, dry_run: bool) -> None:
    """Print human-readable summary."""
    action = "Would delete" if dry_run else "Deleted"
    freed_mb = result["freed_bytes"] / (1024 * 1024)

    print(f"\n{'─' * 50}")
    print(f"  {action}: {result['deleted']} files")
    if result["deleted"] > 0:
        print(f"  Freed: {freed_mb:.1f} MB")
    if result["errors"]:
        print(f"  Errors: {len(result['errors'])}")
        for e in result["errors"][:5]:
            print(f"    - {e}")
    print(f"{'─' * 50}")

    # Health check: warn if cache is empty (might indicate pipeline failure)
    project_root = _get_project_root()
    for exp_dir_rel in EXPIRY_RULES:
        exp_dir = project_root / exp_dir_rel
        if exp_dir.exists():
            files = [f for f in exp_dir.rglob("*") if f.is_file() and f.name != ".gitkeep"]
            if not files:
                logger.warning("Cache directory is empty: %s — pipeline may have stalled", exp_dir_rel)


# ============================================================================
# CLI
# ============================================================================

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Cache cleaner — TTL-based expiry with whitelist protection"
    )
    parser.add_argument("--dry-run", action="store_true", default=False,
                        help="Preview without deleting (default: %(default)s)")
    parser.add_argument("--verbose", "-v", action="store_true", default=False,
                        help="Log each file decision")
    parser.add_argument("--project-root", type=str, default=None,
                        help="Project root directory (default: auto-detect)")
    parser.add_argument("--json", action="store_true", default=False,
                        help="Output result as JSON")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s: %(message)s",
    )

    project_root = Path(args.project_root) if args.project_root else _get_project_root()
    if not project_root.exists():
        print(f"ERROR: Project root not found: {project_root}", file=sys.stderr)
        sys.exit(1)

    if args.dry_run:
        logger.info("DRY RUN — no files will be deleted")

    result = run_cleanup(project_root, dry_run=args.dry_run, verbose=args.verbose)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print_summary(result, dry_run=args.dry_run)

    if result["errors"]:
        sys.exit(2)


if __name__ == "__main__":
    main()
