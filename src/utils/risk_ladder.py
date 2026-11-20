"""Balance-tiered risk ladder.

Prop accounts don't want one flat risk percentage: how much you may lose on the
next trade depends on how far the account has drifted from the balance it
started with. Down near the stop-out you risk less; up in profit you risk more.

The ladder is expressed against ``initial_balance`` — the account's starting
balance, which a prop firm may *raise* after a scale-up. That is deliberately a
configured number and not the first balance we happen to observe: after a
scale-up the old first-seen value would be wrong, and the whole ladder with it.

Default ladder (what the tiers mean, in the operator's words):

    balance < -2.5% of initial  ->  risk 0.20% per trade
    balance within +-2.5%       ->  risk 0.25% per trade
    balance > +2.5% of initial  ->  risk 0.30% per trade

A tier is ``(up_to_pct, risk_pct)``: it applies while the account's drift from
initial balance is <= ``up_to_pct``. The last tier carries ``None`` — open
ended, the "and everything above" bucket. Tiers are evaluated low to high and
the first match wins.
"""

from __future__ import annotations

# (up_to_pct, risk_pct); up_to_pct None == open ended top tier
RiskTier = tuple[float | None, float]

DEFAULT_TIERS: list[RiskTier] = [
    (-2.5, 0.20),
    (2.5, 0.25),
    (None, 0.30),
]


def default_tiers_as_dicts() -> list[dict]:
    """Default ladder in the shape config.yaml / the GUI store it in."""
    return [{"up_to_pct": up, "risk_pct": risk} for up, risk in DEFAULT_TIERS]


def normalize_tiers(raw) -> list[RiskTier]:
    """Coerce whatever came out of YAML / the GUI table into a usable ladder.

    Accepts dicts (``{"up_to_pct": -2.5, "risk_pct": 0.2}``) or 2-sequences.
    Rows with a non-positive risk are dropped — a 0% tier is never something
    the user meant, it's a half-filled table row. Returns [] if nothing
    survives, which callers read as "no ladder, use the static risk_pct".
    """
    if not raw:
        return []

    tiers: list[RiskTier] = []
    for row in raw:
        if isinstance(row, dict):
            up = row.get("up_to_pct")
            risk = row.get("risk_pct")
        elif isinstance(row, (list, tuple)) and len(row) == 2:
            up, risk = row
        else:
            continue
        try:
            risk = float(risk)
        except (TypeError, ValueError):
            continue
        if risk <= 0:
            continue
        if up is None or up == "":
            up = None
        else:
            try:
                up = float(up)
            except (TypeError, ValueError):
                continue
        tiers.append((up, risk))

    if not tiers:
        return []

    # Low to high, open-ended tier last regardless of where it was written.
    tiers.sort(key=lambda t: (t[0] is None, t[0] if t[0] is not None else 0.0))

    # Guarantee a top bucket. Without one, a balance above every threshold
    # would fall through with no risk at all — silently sizing nothing.
    if tiers[-1][0] is not None:
        tiers.append((None, tiers[-1][1]))

    return tiers


def resolve_risk_pct(
    initial_balance: float,
    current_value: float,
    tiers,
    fallback_pct: float,
) -> tuple[float, str]:
    """Pick the risk % for the next trade from the ladder.

    Args:
        initial_balance: account's starting (or scaled-up) balance. <= 0
            disables the ladder — there is nothing to measure drift against.
        current_value: live balance or equity, whichever base the slave uses.
        tiers: raw or normalized ladder (see :func:`normalize_tiers`).
        fallback_pct: the slave's static ``risk_pct``, used whenever the
            ladder can't be applied.

    Returns:
        ``(risk_pct, reason)`` — reason is a short human string for logs,
        traces and the toast tooltip, never for parsing.
    """
    if initial_balance <= 0:
        return fallback_pct, "static (no initial balance)"
    if current_value <= 0:
        return fallback_pct, "static (no account value)"

    ladder = normalize_tiers(tiers)
    if not ladder:
        return fallback_pct, "static (no tiers)"

    drift_pct = (current_value - initial_balance) / initial_balance * 100.0

    for up_to, risk in ladder:
        if up_to is None or drift_pct <= up_to:
            band = "<= %+.2f%%" % up_to if up_to is not None else "above top tier"
            return risk, f"{drift_pct:+.2f}% of initial ({band}) -> {risk:.2f}%"

    # Unreachable: normalize_tiers always appends an open-ended tier.
    return fallback_pct, "static (ladder exhausted)"