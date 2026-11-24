"""Paper-only strategy presets.

The default preset is liquid_large_cap_iex_v2: a free-IEX large-cap/ETF
first-pullback retuned so AAPL/MSFT/SPY actually produce historical setups.
liquid_large_cap_iex_v1 is kept intact (0 IEX trades historically) for
comparison. The small-cap float/news profile remains gated off.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping

# Names already used by the approved IEX Market Watch connection test.
LIQUID_IEX_UNIVERSE: tuple[str, ...] = ("AAPL", "MSFT", "SPY")

PRESET_LIQUID_IEX_V1 = "liquid_large_cap_iex_v1"
PRESET_LIQUID_IEX = "liquid_large_cap_iex_v2"  # default (v2)
PRESET_LIQUID_IEX_V2 = PRESET_LIQUID_IEX
PRESET_SMALL_CAP_CATALYST = "small_cap_catalyst_v1"

# Small-cap keeps the original experimental_v1 profile id.
PROFILE_LIQUID_IEX_V1 = "liquid_large_cap_iex_v1"
PROFILE_LIQUID_IEX = "liquid_large_cap_iex_v2"
PROFILE_LIQUID_IEX_V2 = PROFILE_LIQUID_IEX
PROFILE_SMALL_CAP = "experimental_v1"
APPROVED_PAPER_PROFILES = frozenset({
    PROFILE_LIQUID_IEX_V1, PROFILE_LIQUID_IEX_V2, PROFILE_SMALL_CAP,
})

# Fixture feeds used by the synthetic demo and unit tests. They never satisfy
# a paper data-feed check, which only allows iex or sip.
SYNTHETIC_FEEDS = frozenset({"demo", "synthetic"})


@dataclass(frozen=True)
class PresetSpec:
    preset_id: str
    profile: str
    title: str
    description: str
    default_watchlist: tuple[str, ...]
    required_feed: str
    requires_float_catalyst: bool
    gated: bool
    gate_reasons: tuple[str, ...]
    strategy_defaults: Mapping[str, object]


# Exact PR#9 liquid_large_cap_iex_v1 thresholds (kept for comparison; 0 IEX trades).
LIQUID_IEX_V1_DEFAULTS: dict[str, object] = {
    "profile": PROFILE_LIQUID_IEX_V1,
    "preset": PRESET_LIQUID_IEX_V1,
    "min_price": Decimal("5"),
    "max_price": Decimal("1000"),
    "min_gain_pct": Decimal("0.5"),
    "min_rvol": Decimal("1.5"),
    "max_float_exclusive": 20_000_000,
    "min_impulse_gain_pct": Decimal("0.3"),
    "min_impulse_mean_volume": 50_000,
    "min_impulse_body_fraction": Decimal("0.5"),
    "max_pullback_volume_ratio": Decimal("0.6"),
    "max_retracement": Decimal("0.5"),
    "min_pullback_bars": 2,
    "max_pullback_bars": 3,
    "min_green_impulse_bars": 3,
    "require_strict_rising": True,
    "require_target_within_impulse_peak": True,
    "impulse_vol_vs_same_clock_median": None,
    "max_spread": Decimal("0.08"),
    "max_spread_bps": Decimal("40"),
    "trade_start_et": "09:35",
    "trade_end_et": "15:45",
    "requires_float_catalyst": False,
    "allowed_symbols": LIQUID_IEX_UNIVERSE,
}

# Strategy Lab retune proposal (FINDINGS.md "Retuned IEX preset").
# Absolute min_impulse_mean_volume is unused when the relative median gate is set;
# value 1 satisfies integer validation only.
LIQUID_IEX_V2_DEFAULTS: dict[str, object] = {
    "profile": PROFILE_LIQUID_IEX_V2,
    "preset": PRESET_LIQUID_IEX_V2,
    "min_price": Decimal("5"),
    "max_price": Decimal("1000"),
    "min_gain_pct": Decimal("0.10"),
    "min_rvol": Decimal("1.2"),
    "max_float_exclusive": 20_000_000,
    "min_impulse_gain_pct": Decimal("0.10"),
    "min_impulse_mean_volume": 1,
    "min_impulse_body_fraction": Decimal("0.25"),
    "max_pullback_volume_ratio": Decimal("1.0"),
    "max_retracement": Decimal("0.65"),
    "min_pullback_bars": 1,
    "max_pullback_bars": 3,
    "min_green_impulse_bars": 2,
    "require_strict_rising": False,
    "require_target_within_impulse_peak": False,
    "impulse_vol_vs_same_clock_median": Decimal("1.2"),
    "max_spread": Decimal("0.08"),
    "max_spread_bps": Decimal("40"),
    "trade_start_et": "09:35",
    "trade_end_et": "15:45",
    "requires_float_catalyst": False,
    "allowed_symbols": LIQUID_IEX_UNIVERSE,
}

# Back-compat alias used by older tests / docs that imported LIQUID_IEX_DEFAULTS.
LIQUID_IEX_DEFAULTS = LIQUID_IEX_V2_DEFAULTS

PRESETS: dict[str, PresetSpec] = {
    PRESET_LIQUID_IEX_V2: PresetSpec(
        preset_id=PRESET_LIQUID_IEX_V2,
        profile=PROFILE_LIQUID_IEX_V2,
        title="Liquid large-cap / ETF pullback (IEX v2, retuned)",
        description=(
            "Intraday first-pullback on AAPL, MSFT, SPY for free Alpaca IEX. "
            "Impulse volume is gated vs the same-clock IEX bar-volume median "
            "(1.2x), with loosened impulse/pullback shape so liquid names can "
            "fire. Research proposal from Strategy Lab; not paper-armed."
        ),
        default_watchlist=LIQUID_IEX_UNIVERSE,
        required_feed="iex",
        requires_float_catalyst=False,
        gated=False,
        gate_reasons=(),
        strategy_defaults=LIQUID_IEX_V2_DEFAULTS,
    ),
    PRESET_LIQUID_IEX_V1: PresetSpec(
        preset_id=PRESET_LIQUID_IEX_V1,
        profile=PROFILE_LIQUID_IEX_V1,
        title="Liquid large-cap / ETF pullback (IEX v1, legacy)",
        description=(
            "Original PR#9 liquid thresholds. Kept for comparison; Strategy Lab "
            "measured 0 trades over ~12 months of free IEX on AAPL/MSFT/SPY "
            "(min_impulse_mean_volume=50000 is SIP-scale)."
        ),
        default_watchlist=LIQUID_IEX_UNIVERSE,
        required_feed="iex",
        requires_float_catalyst=False,
        gated=False,
        gate_reasons=(),
        strategy_defaults=LIQUID_IEX_V1_DEFAULTS,
    ),
    PRESET_SMALL_CAP_CATALYST: PresetSpec(
        preset_id=PRESET_SMALL_CAP_CATALYST,
        profile=PROFILE_SMALL_CAP,
        title="Small-cap first pullback + float/news (gated)",
        description=(
            "Original experimental small-cap first-pullback: float under 20 million, "
            "a confirmed news catalyst, 5x relative volume, and a $2–$20 band. "
            "Disabled unless enable_gated_presets is true. Paper execution also needs "
            "SIP (or an equivalent full tape), point-in-time float metadata, catalyst "
            "evidence, and a market-wide scanner. Free IEX cannot enable this preset."
        ),
        default_watchlist=(),
        required_feed="sip",
        requires_float_catalyst=True,
        gated=True,
        gate_reasons=(
            "requires_sip_or_full_tape",
            "requires_point_in_time_float_metadata",
            "requires_catalyst_evidence",
            "requires_market_wide_scanner",
            "explicit_gate_not_enabled",
        ),
        strategy_defaults={
            "profile": PROFILE_SMALL_CAP,
            "preset": PRESET_SMALL_CAP_CATALYST,
            "min_price": Decimal("2"),
            "max_price": Decimal("20"),
            "min_gain_pct": Decimal("10"),
            "min_rvol": Decimal("5"),
            "max_float_exclusive": 20_000_000,
            "min_impulse_gain_pct": Decimal("2"),
            "min_impulse_mean_volume": 10_000,
            "min_impulse_body_fraction": Decimal("0.5"),
            "max_pullback_volume_ratio": Decimal("0.6"),
            "max_retracement": Decimal("0.5"),
            "min_pullback_bars": 2,
            "max_pullback_bars": 3,
            "min_green_impulse_bars": 3,
            "require_strict_rising": True,
            "require_target_within_impulse_peak": True,
            "impulse_vol_vs_same_clock_median": None,
            "max_spread": Decimal("0.05"),
            "max_spread_bps": Decimal("100"),
            "trade_start_et": "07:00",
            "trade_end_et": "10:00",
            "requires_float_catalyst": True,
            "allowed_symbols": (),
        },
    ),
}


def get_preset(preset_id: str | None = None) -> PresetSpec:
    key = preset_id or PRESET_LIQUID_IEX
    if key not in PRESETS:
        raise ValueError(f"Unknown strategy preset: {key}")
    return PRESETS[key]


def default_watchlist_for_preset(preset_id: str | None = None) -> tuple[str, ...]:
    return get_preset(preset_id).default_watchlist
