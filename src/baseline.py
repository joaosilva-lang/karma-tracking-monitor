"""Pure helpers for window counts and the weekday-median baseline.

Everything here operates on {iso_date: count} maps and stdlib types only (no
API clients), so it can be unit-tested with synthetic data.
"""
from datetime import date, timedelta
from math import ceil
from statistics import median

BASELINE_DAYS = 90
# Relative (WARN) checks only apply to events whose weekday median is at least
# this — below it, day-to-day Poisson noise makes % comparisons meaningless.
BASELINE_MIN_MEDIAN = 10
BASELINE_THRESHOLD_DEFAULT = 0.5

# Value checks only apply to events whose history proves they carry value:
# value>0 on >= VALUE_CARRYING_PCT of the days they fired, with at least
# MIN_VALUE_DAYS fired days of evidence.
MIN_VALUE_DAYS = 10
VALUE_CARRYING_PCT = 0.80

# Suggested goback_days = the longest observed dry spell x a safety factor,
# clamped: never tighter than 3 days, never wider than the analysis window.
GOBACK_SUGGEST_MIN = 3
GOBACK_SUGGEST_MAX = 90
GOBACK_SUGGEST_FACTOR = 1.5


def normalize_date(value: str) -> str:
    """GA4 returns dates as YYYYMMDD, GAds as YYYY-MM-DD. Normalize to ISO."""
    v = str(value).strip()
    if len(v) == 8 and v.isdigit():
        return f"{v[0:4]}-{v[4:6]}-{v[6:8]}"
    return v


def date_range(end: date, days: int) -> list[str]:
    """ISO dates for the `days`-day window ending at `end` (inclusive), oldest first."""
    return [(end - timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]


def window_count(day_map: dict[str, float], end: date, days: int) -> float:
    """Total count in the `days`-day window ending at `end` (inclusive)."""
    return sum(day_map.get(d, 0) for d in date_range(end, days))


def weekday_median(day_map: dict[str, float], test_day: date,
                   lookback_days: int = BASELINE_DAYS) -> float:
    """Median count on test_day's weekday over the lookback window strictly
    before test_day (~12 samples for 90 days). Days with no data count as 0."""
    samples = []
    d = test_day - timedelta(days=7)
    start = test_day - timedelta(days=lookback_days)
    while d >= start:
        samples.append(day_map.get(d.isoformat(), 0))
        d -= timedelta(days=7)
    return float(median(samples)) if samples else 0.0


def per_weekday_medians(day_map: dict[str, float], dates: list[str]) -> list[float]:
    """Median per weekday (index 0=Mon .. 6=Sun) over the given ISO dates."""
    buckets: dict[int, list[float]] = {i: [] for i in range(7)}
    for d in dates:
        buckets[date.fromisoformat(d).weekday()].append(day_map.get(d, 0))
    return [float(median(buckets[i])) if buckets[i] else 0.0 for i in range(7)]


def parse_threshold(raw, default: float = BASELINE_THRESHOLD_DEFAULT) -> float:
    """Accepts '50', '50%' or '0.5' (all meaning 50%). Empty/invalid -> default."""
    if raw is None:
        return default
    s = str(raw).strip().rstrip("%").replace(",", ".")
    if not s:
        return default
    try:
        value = float(s)
    except ValueError:
        return default
    return value / 100 if value > 1 else value


def pct_days_with_value(counts_map: dict[str, float], values_map: dict[str, float],
                        dates: list[str]) -> tuple[int, float]:
    """Returns (fired_days, pct): on the days the event fired within `dates`,
    the fraction of them that also carried value > 0."""
    fired_days = [d for d in dates if counts_map.get(d, 0) > 0]
    if not fired_days:
        return 0, 0.0
    with_value = sum(1 for d in fired_days if values_map.get(d, 0) > 0)
    return len(fired_days), with_value / len(fired_days)


def is_value_carrying(counts_map: dict[str, float], values_map: dict[str, float],
                      dates: list[str]) -> bool:
    """True when history proves the event consistently carries value, so a
    day with count>0 and value==0 is genuinely anomalous."""
    fired_days, pct = pct_days_with_value(counts_map, values_map, dates)
    return fired_days >= MIN_VALUE_DAYS and pct >= VALUE_CARRYING_PCT


def max_zero_gap(day_map: dict[str, float], dates: list[str]) -> int:
    """Longest run of consecutive zero-count days strictly BETWEEN the first
    and last firing day in `dates` (oldest first). Leading/trailing zeros are
    excluded on purpose: they aren't gaps between firings — a trailing run is
    either attribution lag (GAds) or an ongoing outage, and neither should
    inflate the suggested lookback. An event that never fired returns
    len(dates) (dead for the whole window)."""
    firing_idx = [i for i, d in enumerate(dates) if day_map.get(d, 0) > 0]
    if not firing_idx:
        return len(dates)
    gap = 0
    for prev, nxt in zip(firing_idx, firing_idx[1:]):
        gap = max(gap, nxt - prev - 1)
    return gap


def suggest_goback_days(max_gap: int) -> int:
    """Suggested goback_days for the wide check: the longest observed dry
    spell plus a proportional margin, clamped to [GOBACK_SUGGEST_MIN,
    GOBACK_SUGGEST_MAX]. Zero false positives on the observed history by
    construction (window always exceeds every gap actually seen)."""
    return min(GOBACK_SUGGEST_MAX,
               max(GOBACK_SUGGEST_MIN, ceil(max_gap * GOBACK_SUGGEST_FACTOR)))


def short_check_status(count: float, expected: float, threshold: float) -> str:
    """FAIL on zero; WARN when a baseline-eligible event (expected >=
    BASELINE_MIN_MEDIAN) falls below threshold*expected; OK otherwise."""
    if count == 0:
        return "FAIL"
    if expected >= BASELINE_MIN_MEDIAN and count < threshold * expected:
        return "WARN"
    return "OK"
