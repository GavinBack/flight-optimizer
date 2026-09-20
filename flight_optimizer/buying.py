"""What it costs to buy the miles you are short: retail prices, and (later) promo prices.

Retail prices come from secondary sources read on 2026-09-20 (WalletHub, AwardWallet,
One Mile at a Time, LoyaltyLobby, Upgraded Points, Head for Points and others). The
programs' own pages were not read, and prices change, so every figure here is an
ESTIMATE to check on the program's site before paying. Cross-checks that held:

- Delta's 2026 promo, 60,000 points for $2,257.50, is exactly 60,000 x 3.5 cents x 1.075.
- Aeroplan's 100% bonus tier at 1.88 CAD cents is exactly half its 3.75 CAD cent base.
- JetBlue's 1,000 points for $32.25 is exactly 3.0 cents x 1.075.

Left out on purpose: Qantas (only third-party brokers sell cheaply, which violates its
terms) and Southwest (Seats.aero has no Southwest availability, so a gap there cannot arise).
"""

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Iterable

from flight_optimizer.affordability import TripOption
from flight_optimizer.promos import Promo

US_EXCISE_TAX = 0.075  # federal excise tax on miles bought from US programs

ESTIMATE_NOTE = "prices are estimates from secondary sources: confirm on the program's site before paying"


@dataclass(frozen=True)
class RetailPrice:
    program: str  # balance key, e.g. "delta"
    cents_per_point: float  # US cents per point, before tax, no promo
    tax_rate: float  # 0.075 for US federal excise tax; 0 where none is known to apply
    min_points: int
    step: int  # purchases must be a whole number of these (above the minimum)
    annual_cap: int | None  # base points per calendar year; None if not known
    confidence: str  # "medium" (several sources agree) or "low" (one source, or FX/tiered pricing)
    note: str


RETAIL_PRICES: dict[str, RetailPrice] = {
    p.program: p
    for p in (
        RetailPrice("delta", 3.5, US_EXCISE_TAX, 2_000, 2_000, 60_000, "medium",
                    "3.76 cents including tax. Block size of 2,000 is from one source."),
        RetailPrice("united", 3.5, US_EXCISE_TAX, 2_000, 1_000, 200_000, "medium",
                    "Also capped at 50,000 miles per rolling 90 days."),
        RetailPrice("american", 3.5, US_EXCISE_TAX, 2_000, 1_000, 300_000, "medium",
                    "Sources quote 2.25 to 3.76 cents depending on quantity and current discounts."),
        RetailPrice("atmos", 3.5, US_EXCISE_TAX, 1_000, 1_000, 150_000, "medium",
                    "Sells to some members at a discount; the price shown to you may differ."),
        RetailPrice("jetblue", 3.0, US_EXCISE_TAX, 1_000, 1_000, 300_000, "medium",
                    "3.225 cents per point including tax, from $32.25 per 1,000."),
        RetailPrice("aeroplan", 2.7, 0.0, 2_000, 1_000, 1_000_000, "low",
                    "3.75 Canadian cents per point, converted at roughly 0.72; moves with the exchange rate."),
        RetailPrice("lifemiles", 3.3, 0.0, 1_000, 1_000, 500_000, "medium",
                    "$33 per 1,000 excluding applicable taxes; whether tax is added is not known."),
        RetailPrice("british", 3.25, 0.0, 2_000, 1_000, 200_000, "low",
                    "Tiered: 3.25 cents for small purchases, down to about 2.3 at 200,000. Block sizes vary by range."),
        RetailPrice("flyingblue", 3.05, 0.0, 2_000, 2_000, None, "low",
                    "Priced in euros; the dollar figure moves with the exchange rate."),
        RetailPrice("virginatlantic", 2.5, 0.0, 1_000, 1_000, 200_000, "low",
                    "$25 per 1,000 points for a US-registered account."),
    )
}


# An alert needs a sale at least this much cheaper than retail (owner's rule: alert only when the
# effective price is below a threshold). LifeMiles' 1.5 cents is the README's own example.
PROMO_ALERT_FRACTION = 0.70
PROMO_THRESHOLDS_CENTS: dict[str, float] = {"lifemiles": 1.5}


def promo_threshold_cents(program: str) -> float | None:
    """The most, in cents per point, a sale may cost per point to be worth an alert."""
    if program in PROMO_THRESHOLDS_CENTS:
        return PROMO_THRESHOLDS_CENTS[program]
    price = RETAIL_PRICES.get(program)
    return None if price is None else price.cents_per_point * (1 + price.tax_rate) * PROMO_ALERT_FRACTION


@dataclass(frozen=True)
class Quote:
    """The cheapest purchase that covers a shortfall."""

    program: str
    points_needed: int
    points_bought: int  # base points paid for (rounded up to the program's block size)
    points_received: int  # what lands in the account, including any bonus
    cost_usd: float  # including tax
    effective_cents_per_point: float  # cost / points received
    confidence: str
    note: str = ""
    kind: str = "retail"  # "retail", or the promo's kind: "bonus" / "discount"
    percent: int = 0  # the bonus or discount this purchase earns
    tiers_known: bool = True  # False: priced from a promo's headline only, a best case
    promo_title: str = ""
    promo_url: str = ""
    promo_ends: date | None = None


def _round_up_purchase(price: RetailPrice, points: int) -> int:
    """The smallest allowed purchase of at least `points`."""
    if points <= price.min_points:
        return price.min_points
    blocks = math.ceil((points - price.min_points) / price.step)
    return price.min_points + blocks * price.step


def _retail_quote(program: str, points_needed: int) -> Quote | None:
    price = RETAIL_PRICES.get(program)
    if price is None or points_needed <= 0:
        return None
    bought = _round_up_purchase(price, points_needed)
    if price.annual_cap is not None and bought > price.annual_cap:
        return None
    cost = round(bought * price.cents_per_point / 100 * (1 + price.tax_rate), 2)
    return Quote(
        program=program, points_needed=points_needed, points_bought=bought, points_received=bought,
        cost_usd=cost, effective_cents_per_point=cost / bought * 100, confidence=price.confidence,
        note=price.note,
    )


def _promo_quote(program: str, points_needed: int, promo: Promo) -> Quote | None:
    """Price the same shortfall under a sale: the smallest purchase that lands enough points.

    A bonus adds points on top of what you buy; a discount lowers the price. Tiers are read
    at the size of the *purchase* (so a small top-up gets the lowest tier); with no tiers the
    headline is used as a best case and the quote says so.
    """
    price = RETAIL_PRICES.get(program)
    if price is None or points_needed <= 0:
        return None
    limit = price.annual_cap if price.annual_cap is not None else 1_000_000
    base = price.min_points
    while base <= limit:
        percent = promo.percent_for(base)
        if promo.kind == "bonus":
            received = base + base * percent // 100
            cost = base * price.cents_per_point / 100 * (1 + price.tax_rate)
        else:
            received = base
            cost = base * price.cents_per_point / 100 * (1 - percent / 100) * (1 + price.tax_rate)
        if received >= points_needed:
            cost = round(cost, 2)
            return Quote(
                program=program, points_needed=points_needed, points_bought=base, points_received=received,
                cost_usd=cost, effective_cents_per_point=cost / received * 100, confidence=price.confidence,
                note=price.note, kind=promo.kind, percent=percent, tiers_known=promo.tiers_known,
                promo_title=promo.title, promo_url=promo.url, promo_ends=promo.ends,
            )
        base += price.step
    return None


def quote_purchase(
    program: str, points_needed: int, promo: Promo | None = None, today: date | None = None
) -> Quote | None:
    """Price buying `points_needed`: the retail price, or the sale price if `promo` is a live
    sale for this program and is cheaper. None if it can't be bought or exceeds the annual limit."""
    retail = _retail_quote(program, points_needed)
    if promo is None or promo.program != program or (today is not None and not promo.is_active(today)):
        return retail
    sale = _promo_quote(program, points_needed, promo)
    if sale is None:
        return retail
    return sale if retail is None or sale.cost_usd < retail.cost_usd else retail


@dataclass(frozen=True)
class Purchase:
    program: str
    shortfall: int
    quote: Quote  # the cheaper of retail and any live sale
    retail_quote: Quote  # what the same gap costs at the normal price, for comparison


@dataclass(frozen=True)
class BuyPlan:
    """An unaffordable option, and what buying its shortfalls would cost."""

    option: TripOption
    purchases: tuple[Purchase, ...] = field(default_factory=tuple)

    @property
    def total_cost(self) -> float:
        return round(sum(p.quote.cost_usd for p in self.purchases), 2)

    @property
    def retail_cost(self) -> float:
        return round(sum(p.retail_quote.cost_usd for p in self.purchases), 2)

    @property
    def uses_promo(self) -> bool:
        return any(p.quote.kind != "retail" for p in self.purchases)


def plan_purchases(
    option: TripOption, promos: dict[str, Promo] | None = None, today: date | None = None
) -> BuyPlan | None:
    """What it would cost to buy an option's way to affordability, or None if it can't be done.

    Only handles options that miss purely because a program balance is short. A short
    Capital One pool, or a program we can't price, means buying isn't a clean fix.
    """
    a = option.affordability
    if a.affordable or a.capital_one_short or a.unknown_sources or not a.shortfalls:
        return None
    promos = promos or {}
    purchases = []
    for program, short in sorted(a.shortfalls.items()):
        retail = _retail_quote(program, short)
        if retail is None:
            return None
        purchases.append(Purchase(program, short, quote_purchase(program, short, promos.get(program), today), retail))
    return BuyPlan(option=option, purchases=tuple(purchases))


def rank_buy_plans(
    options: Iterable[TripOption], today: date, promos: dict[str, Promo] | None = None
) -> list[BuyPlan]:
    """Buy plans for every near-miss that buying can fix: current data first, then cheapest."""
    plans = [plan for option in options if (plan := plan_purchases(option, promos, today)) is not None]
    return sorted(
        plans,
        key=lambda p: (
            int(p.option.freshness(today)), p.total_cost, p.option.self_transfers,
            p.option.connection_penalty, p.option.stop_penalty, p.option.total_miles,
        ),
    )
