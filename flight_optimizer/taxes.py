"""Award taxes and fees, in dollars, so an award can be compared with a cash fare.

Seats.aero reports each flight's `TotalTaxes` in hundredths of its `TaxesCurrency` (confirmed for CAD;
see `seats_aero.TAX_UNIT_CONFIRMED`). To add them to an award's cost they are converted to US dollars at
the FIXED, APPROXIMATE rates below: change them here when they drift. A currency without a rate makes an
option's taxes incomplete, and the report says so instead of guessing.

A leg reporting zero taxes counts as zero: Seats.aero cannot tell "free" from "unknown", so a few awards
may look cheaper than they are.
"""

from dataclasses import dataclass

from flight_optimizer.seats_aero import TAX_UNIT_CONFIRMED

# US dollars per one unit of the currency. Approximate and fixed, not live rates.
USD_PER_UNIT: dict[str, float] = {"USD": 1.00, "CAD": 0.72, "AUD": 0.65, "EUR": 1.08, "GBP": 1.27}


@dataclass(frozen=True)
class Taxes:
    usd: float  # the taxes that could be converted
    complete: bool  # False if some leg's currency has no rate, so `usd` is a floor
    unchecked: tuple[str, ...]  # currencies used whose "hundredths" unit was not confirmed

    @property
    def text(self) -> str:
        """e.g. "$305 taxes", "$305+ taxes" when part is unknown."""
        return f"${self.usd:,.0f}{'' if self.complete else '+'} taxes"


def leg_taxes_usd(leg) -> float | None:
    """One leg's taxes in dollars, or None if its currency has no rate."""
    if not leg.total_taxes:
        return 0.0
    rate = USD_PER_UNIT.get(leg.currency)
    return None if rate is None else leg.total_taxes / 100 * rate


def option_taxes(option) -> Taxes:
    """Taxes for every leg of a trip option, in dollars."""
    total, complete, unchecked = 0.0, True, set()
    for itinerary in option.itineraries:
        for leg in itinerary.legs:
            usd = leg_taxes_usd(leg)
            if usd is None:
                complete = False
                continue
            total += usd
            if leg.total_taxes and leg.currency not in TAX_UNIT_CONFIRMED:
                unchecked.add(leg.currency)
    return Taxes(usd=total, complete=complete, unchecked=tuple(sorted(unchecked)))


def rates_note() -> str:
    """One line for the report and plans: what the conversion assumes."""
    rates = ", ".join(f"{cur} {rate:g}" for cur, rate in USD_PER_UNIT.items() if cur != "USD")
    return (
        f"Taxes and fees are converted to dollars at fixed approximate rates ({rates}); only CAD is confirmed as "
        "hundredths of the currency, the others are assumed."
    )
