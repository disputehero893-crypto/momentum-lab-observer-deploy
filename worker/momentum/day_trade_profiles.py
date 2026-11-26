"""Registry and gates for the opt-in stock day-trade profiles (paper only).

``DAY_TRADE_PROFILE`` selects one profile. Empty (the default) means no profile:
the worker keeps trading the default ``liquid_large_cap_iex_v2`` strategy and
the reviewed config hash is byte-for-byte what it was before this module.

Selecting a profile:
* adds a ``day_trade_profile`` block to ``Config.review_hash()``, so the paper
  hash changes and Derek has to approve the new hash;
* adds paper blockers until the profile can really run. In this release the
  runtime loop is not wired to the profiles yet (that touches the files owned by
  PR #57), so a selected profile blocks stock paper entries with
  ``day_trade_profile_runtime_not_wired`` instead of silently trading v2.
* ``ross_cameron_small_cap_v1`` is also hard-gated on float, news-catalyst and
  full-market gainers data plus a SIP feed.
* ``small_cap_candle_patterns_iex_v1`` runs on free data (IEX feed, Alpaca
  movers + news, FMP float): it needs the float provider and the movers
  scanner; news is optional and the feed may be ``iex``.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Iterable

from . import large_cap_day_trade_v1 as large_cap
from . import ross_cameron_small_cap_v1 as ross
from . import small_cap_candle_patterns_iex_v1 as candle

LARGE_CAP = large_cap.PROFILE_ID
ROSS_CAMERON = ross.PROFILE_ID
CANDLE_IEX = candle.PROFILE_ID
PROFILE_IDS = (LARGE_CAP, ROSS_CAMERON, CANDLE_IEX)
DEFAULT_PROFILE = ""  # off
# Flipped only by the reviewed follow-up that wires the engines into the paper loop.
RUNTIME_WIRED = False
REFERENCE_EQUITY = Decimal("100000")


def normalize(value: Any) -> str:
    name = str(value or "").strip().lower()
    if name in {"", "off", "none"}:
        return ""
    if name not in PROFILE_IDS:
        raise ValueError(f"DAY_TRADE_PROFILE must be empty or one of {', '.join(PROFILE_IDS)}")
    return name


def _risk(risk_overrides: dict | None):
    from .risk import RiskConfig
    return RiskConfig(**(risk_overrides or {}))


def hash_settings(profile: str, risk_overrides: dict | None, data_feed: str) -> dict:
    """The block added to the reviewed config hash when a profile is selected."""
    rc = _risk(risk_overrides)
    if profile == LARGE_CAP:
        params = large_cap.LargeCapV1Params()
        caps = large_cap.effective_caps(params, rc, REFERENCE_EQUITY).public()
        universe = list(large_cap.UNIVERSE)
    elif profile == ROSS_CAMERON:
        params = ross.RossV1Params()
        caps = ross.ross_caps(params, rc).public()
        universe = ["full_market_scanner"]
    elif profile == CANDLE_IEX:
        params = candle.CandleIexV1Params()
        caps = candle.candle_caps(params, rc).public()
        universe = ["alpaca_movers_scanner"]
    else:
        raise ValueError("no day-trade profile selected")
    return {"profile": profile, "profile_config_id": params.config_id, "effective_caps": caps,
            "universe": universe, "feed": data_feed, "entry_extended_hours": False,
            "limit_ttl_seconds": params.limit_ttl_seconds, "risk_config_id": rc.config_id}


def blockers(profile: str, *, watchlist: Iterable[str] = (), data_feed: str = "iex",
             providers: Any = None) -> list[str]:
    """Reasons a selected profile cannot place paper entries. Empty only when it truly can."""
    if not profile:
        return []
    reasons = []
    if not RUNTIME_WIRED:
        reasons.append("day_trade_profile_runtime_not_wired")
    if profile == LARGE_CAP:
        symbols = set(watchlist)
        if not symbols or not symbols <= set(large_cap.UNIVERSE):
            reasons.append("large_cap_v1_watchlist_must_be_aapl_msft_spy")
        if symbols & set(large_cap.SPY_ALIGNED) and "SPY" not in symbols:
            reasons.append("large_cap_v1_needs_spy_for_aapl_msft")
        if data_feed != "iex":
            reasons.append("large_cap_v1_reviewed_on_iex_only")
    elif profile == ROSS_CAMERON:
        reasons.extend(ross.provider_blockers(providers, data_feed))
    elif profile == CANDLE_IEX:
        reasons.extend(candle.provider_blockers(providers, data_feed))
    return reasons


def report(profile: str, *, watchlist: Iterable[str] = (), data_feed: str = "iex",
           risk_overrides: dict | None = None, providers: Any = None) -> dict:
    """Plain status for /ready configuration and diagnostics. No secrets."""
    if not profile:
        return {"selected": None, "available": list(PROFILE_IDS), "active": False,
                "default_strategy_unchanged": True}
    why = blockers(profile, watchlist=watchlist, data_feed=data_feed, providers=providers)
    return {"selected": profile, "available": list(PROFILE_IDS), "active": not why, "blockers": why,
            "hash_settings": hash_settings(profile, risk_overrides, data_feed)}
