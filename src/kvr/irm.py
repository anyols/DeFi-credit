"""AdaptiveCurveIRM, replicated integer-exact. Verified against source (MIT), not the spec's
rounded prose constants:
- morpho-org/morpho-blue-irm src/adaptive-curve-irm/AdaptiveCurveIrm.sol (curve, drift, averaging)
- .../libraries/ConstantsLib.sol (every constant below)
- .../libraries/ExpLib.sol (wExp: a bounded 2nd-order Taylor approximation with a power-of-2
  bit-shift decomposition -- deliberately not a true exponential, so T3 needs this exact
  approximation, not math.exp, to hit its 1e-9 tolerance)

Originally slated as a hand-written file (see SPEC.md 1.1); written by Claude Code instead at
the user's explicit request, after flagging the SPEC.md section 8 whiteboard-derivation tradeoff.
"""

import math

from . import morpho_math as mm

WAD = mm.WAD
WAD_INT = WAD
SECONDS_PER_YEAR = 365 * 24 * 60 * 60

CURVE_STEEPNESS = 4 * WAD
ADJUSTMENT_SPEED = (50 * WAD) // SECONDS_PER_YEAR
TARGET_UTILIZATION = 9 * 10**17  # 0.9e18
INITIAL_RATE_AT_TARGET = (4 * 10**16) // SECONDS_PER_YEAR  # 4%/yr
MIN_RATE_AT_TARGET = (10**15) // SECONDS_PER_YEAR  # 0.1%/yr
MAX_RATE_AT_TARGET = (2 * WAD) // SECONDS_PER_YEAR  # 200%/yr

LN_2_INT = 693147180559945309
LN_WEI_INT = -41446531673892822312
WEXP_UPPER_BOUND = 93859467695000404319
WEXP_UPPER_VALUE = 57716089161558943949701069502944508345128422502756744429568


def div_to_zero(a, b):
    """Integer division truncated toward zero, matching Solidity's int256 `/` (Python's `//`
    floors toward -infinity instead, which differs whenever the result would be negative)."""
    q = abs(a) // abs(b)
    return -q if (a < 0) != (b < 0) else q


def w_mul_to_zero(a, b):
    return div_to_zero(a * b, WAD_INT)


def w_div_to_zero(a, b):
    return div_to_zero(a * WAD_INT, b)


def w_exp(x):
    """Exact replica of ExpLib.wExp: NOT a true exponential, a bounded approximation."""
    if x < LN_WEI_INT:
        return 0
    if x >= WEXP_UPPER_BOUND:
        return WEXP_UPPER_VALUE
    rounding_adjustment = -(LN_2_INT // 2) if x < 0 else (LN_2_INT // 2)
    q = div_to_zero(x + rounding_adjustment, LN_2_INT)
    r = x - q * LN_2_INT
    exp_r = WAD_INT + r + (r * r) // WAD_INT // 2
    return exp_r << q if q >= 0 else exp_r >> (-q)


def utilization(total_borrow_assets, total_supply_assets):
    if total_supply_assets == 0:
        return 0
    return mm.mul_div_down(total_borrow_assets, WAD, total_supply_assets)  # wDivDown, unsigned


def compute_err(u, target=TARGET_UTILIZATION):
    norm = (WAD - target) if u > target else target
    return w_div_to_zero(u - target, norm)


def curve(rate_at_target, err):
    if err < 0:
        coeff = WAD - w_div_to_zero(WAD, CURVE_STEEPNESS)
    else:
        coeff = CURVE_STEEPNESS - WAD
    inner = w_mul_to_zero(coeff, err) + WAD
    return w_mul_to_zero(inner, rate_at_target)


def new_rate_at_target(start_rate_at_target, linear_adaptation):
    if linear_adaptation == 0:
        return start_rate_at_target
    raw = w_mul_to_zero(start_rate_at_target, w_exp(linear_adaptation))
    return min(max(raw, MIN_RATE_AT_TARGET), MAX_RATE_AT_TARGET)


def borrow_rate(u, start_rate_at_target, elapsed):
    """Returns (borrow_rate_per_second_wad, end_rate_at_target). Matches
    AdaptiveCurveIrm._borrowRate exactly: a trapezoidal average of start/mid/end rateAtTarget
    over the elapsed interval, fed into the curve at the current err.
    """
    err = compute_err(u)
    if elapsed == 0:
        return curve(start_rate_at_target, err), start_rate_at_target

    speed = w_mul_to_zero(ADJUSTMENT_SPEED, err)
    linear_adaptation = speed * elapsed
    end_rate_at_target = new_rate_at_target(start_rate_at_target, linear_adaptation)
    mid_rate_at_target = new_rate_at_target(start_rate_at_target, div_to_zero(linear_adaptation, 2))
    avg_rate_at_target = (start_rate_at_target + end_rate_at_target + 2 * mid_rate_at_target) // 4
    return curve(avg_rate_at_target, err), end_rate_at_target


def apy_from_per_second_rate(rate):
    """Float conversion for reporting only (ground rule: float for analytics, int for protocol).
    Continuously-compounded APY -- for a market whose rate is pinned at the IRM's own ceiling
    (sustained ~100% utilization), this blows up to a nonsensical multi-million-percent figure
    (exp() of an already-extreme per-second rate held for a full year), which is a real
    mathematical consequence, not a bug, but not a meaningful "yield" either -- see
    apr_from_per_second_rate for the simple, non-compounded figure a zombie market should be
    reported with instead.
    """
    return math.exp(rate / WAD * SECONDS_PER_YEAR) - 1


def apr_from_per_second_rate(rate):
    """Simple (non-compounded) annualized rate: rate/WAD * SECONDS_PER_YEAR, no exp(). Doesn't
    blow up for a pinned-rate market the way apy_from_per_second_rate does, because it never
    pretends the instantaneous rate compounds continuously for a full year."""
    return rate / WAD * SECONDS_PER_YEAR
