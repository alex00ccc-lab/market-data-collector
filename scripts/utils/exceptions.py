"""Domain-specific exceptions for market data pipeline.

Adapter code raises these; the SourceManager catches and reacts accordingly
(cooldown, fallback, alert).
"""


class MarketDataError(Exception):
    """Base exception for all market data pipeline errors."""
    pass


class RateLimitError(MarketDataError):
    """The data source has rate-limited this IP / API key.

    SourceManager should set a cooldown on the adapter and fall through
    to the next source in the priority chain.
    """
    pass


class GeoBlockError(MarketDataError):
    """The data source is geo-blocking this IP (e.g. East Money from outside CN).

    SourceManager should fall through to the next source.  If all sources for
    a market are geo-blocked, a high-severity alert should fire.
    """
    pass


class KeyInvalidError(MarketDataError):
    """API key is missing, expired, or rejected by the data source.

    SourceManager should log a warning and fall through.  If no sources have
    valid keys, the adapter should be skipped (not retried).
    """
    pass


class AdapterNotAvailableError(MarketDataError):
    """Adapter cannot service this request (unsupported market, missing dep).

    SourceManager should skip this adapter silently.
    """
    pass
