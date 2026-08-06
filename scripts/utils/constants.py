"""Global constants shared across adapters and scripts."""

from datetime import timezone, timedelta

# ── Timezone ──────────────────────────────────────────────────────────────
TZ_BEIJING = timezone(timedelta(hours=8))

# ── HTTP ──────────────────────────────────────────────────────────────────
HTTP_TIMEOUT = 12  # seconds — shared across all adapters

# ── Logging ───────────────────────────────────────────────────────────────
LOG_TRUNCATE_LENGTH = 200  # characters — error message max length in logs
