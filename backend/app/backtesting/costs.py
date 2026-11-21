"""Transaction costs and a capital-gains tax estimate for Indian equity trading.

IMPORTANT: every rate below is an ASSUMPTION, an approximate recent schedule of an Indian discount
broker (zero-brokerage delivery, flat-fee intraday). Rates differ between brokers and have changed over
the years (stamp duty was uniform only from July 2020, exchange charges and STT have moved). They are
defaults you can override, not facts about any particular day. Check your broker's current schedule
before relying on a result, and use the "stress" preset (double costs) to see how fragile a strategy is.

Charges on one order (one "leg"):
  brokerage   percent of value, optionally capped per order, plus an optional flat fee
  STT         securities transaction tax, a percent of value (delivery: both sides; intraday: sell side)
  exchange    exchange transaction charge, percent of value
  SEBI        turnover fee, percent of value (Rs 10 per crore)
  stamp duty  buy side only
  GST         18% of (brokerage + exchange charge + SEBI fee + DP charge)
  DP charge   flat fee per delivery SALE (depository), small but real on every exit

Slippage (the price moving against you between the signal and the fill) is NOT here: the engine applies it
to the fill price (`slippage_bps`).
"""
from dataclasses import asdict, dataclass, replace
from typing import Optional

import pandas as pd

# Capital gains tax: the rates in force from July 2024, applied to ALL years (a simplification).
STCG_RATE = 20.0                 # percent, holding up to 12 months
LTCG_RATE = 12.5                 # percent, holding over 12 months
LTCG_EXEMPTION = 125_000.0       # rupees of long-term gains per financial year
LONG_TERM_DAYS = 365


@dataclass(frozen=True)
class CostModel:
    name: str = "delivery"
    brokerage_pct: float = 0.0
    brokerage_cap: Optional[float] = None          # maximum brokerage per order, rupees
    brokerage_flat: float = 0.0                    # extra flat fee per order, rupees
    stt_buy_pct: float = 0.1
    stt_sell_pct: float = 0.1
    exchange_pct: float = 0.00297
    sebi_pct: float = 0.0001
    stamp_buy_pct: float = 0.015
    gst_pct: float = 18.0
    dp_charge_sell: float = 13.5

    def leg(self, side: str, value: float) -> dict:
        """Itemised cost of ONE order of `value` rupees. side is "buy" or "sell"."""
        if side not in ("buy", "sell"):
            raise ValueError("side must be 'buy' or 'sell'")
        if value < 0:
            raise ValueError("value cannot be negative")
        brokerage = value * self.brokerage_pct / 100
        if self.brokerage_cap is not None:
            brokerage = min(brokerage, self.brokerage_cap)
        brokerage += self.brokerage_flat
        stt = value * (self.stt_buy_pct if side == "buy" else self.stt_sell_pct) / 100
        exchange = value * self.exchange_pct / 100
        sebi = value * self.sebi_pct / 100
        stamp = value * self.stamp_buy_pct / 100 if side == "buy" else 0.0
        dp = self.dp_charge_sell if side == "sell" else 0.0
        gst = (brokerage + exchange + sebi + dp) * self.gst_pct / 100
        total = brokerage + stt + exchange + sebi + stamp + gst + dp
        return {"brokerage": brokerage, "stt": stt, "exchange": exchange, "sebi": sebi, "stamp": stamp,
                "gst": gst, "dp": dp, "total": total}

    def scaled(self, factor: float, name: Optional[str] = None) -> "CostModel":
        """Every charge multiplied by `factor` (2.0 = the stress test)."""
        return replace(self, name=name or f"{self.name}_x{factor:g}", brokerage_pct=self.brokerage_pct * factor,
                       brokerage_cap=None if self.brokerage_cap is None else self.brokerage_cap * factor,
                       brokerage_flat=self.brokerage_flat * factor, stt_buy_pct=self.stt_buy_pct * factor,
                       stt_sell_pct=self.stt_sell_pct * factor, exchange_pct=self.exchange_pct * factor,
                       sebi_pct=self.sebi_pct * factor, stamp_buy_pct=self.stamp_buy_pct * factor,
                       dp_charge_sell=self.dp_charge_sell * factor)

    def to_dict(self) -> dict:
        return asdict(self)


DELIVERY = CostModel(name="delivery")
INTRADAY = CostModel(name="intraday", brokerage_pct=0.03, brokerage_cap=20.0, stt_buy_pct=0.0, stt_sell_pct=0.025,
                     stamp_buy_pct=0.003, dp_charge_sell=0.0)
NO_COSTS = CostModel(name="none", stt_buy_pct=0.0, stt_sell_pct=0.0, exchange_pct=0.0, sebi_pct=0.0,
                     stamp_buy_pct=0.0, gst_pct=0.0, dp_charge_sell=0.0)
PRESETS = {"delivery": DELIVERY, "intraday": INTRADAY, "none": NO_COSTS,
           "stress": DELIVERY.scaled(2.0, "stress")}


def preset(name: str, **overrides) -> CostModel:
    if name not in PRESETS:
        raise ValueError(f"unknown cost preset {name!r}; use one of {sorted(PRESETS)}")
    return replace(PRESETS[name], **overrides) if overrides else PRESETS[name]


def financial_year(ts) -> int:
    """The year a financial year (April to March) STARTS in: 2023-02-01 and 2022-12-01 are both FY 2022."""
    t = pd.Timestamp(ts)
    return t.year if t.month >= 4 else t.year - 1


def estimate_capital_gains_tax(trades: pd.DataFrame) -> dict:
    """A rough income-tax estimate on DELIVERY trades, by financial year. Not tax advice.

    Simplifications: current rates for all years; short-term losses offset long-term gains, long-term
    losses offset only long-term gains; no carry-forward of losses between years; no other income.
    Intraday trades are speculative business income taxed at your slab rate, which is not estimated here.
    """
    if trades.empty:
        return {"total_tax": 0.0, "by_financial_year": {}, "assumptions": _TAX_NOTE}
    t = trades.copy()
    t["fy"] = [financial_year(x) for x in t["exit_ts"]]
    t["long_term"] = t["holding_days"] > LONG_TERM_DAYS
    by_year, total = {}, 0.0
    for fy, g in t.groupby("fy"):
        stcg = float(g.loc[~g["long_term"], "net_pnl"].sum())
        ltcg = float(g.loc[g["long_term"], "net_pnl"].sum())
        if stcg < 0:                       # a short-term loss can reduce long-term gains
            ltcg += stcg
            stcg = 0.0
        ltcg = max(ltcg, 0.0)
        tax = stcg * STCG_RATE / 100 + max(ltcg - LTCG_EXEMPTION, 0.0) * LTCG_RATE / 100
        by_year[f"FY{fy}-{str(fy + 1)[2:]}"] = {"short_term_gain": round(stcg, 2), "long_term_gain": round(ltcg, 2),
                                                 "tax": round(tax, 2)}
        total += tax
    return {"total_tax": round(total, 2), "by_financial_year": by_year, "assumptions": _TAX_NOTE}


_TAX_NOTE = ("rough estimate for delivery trades: 20% short-term and 12.5% long-term (above Rs 1.25 lakh a year) applied to all "
             "years, no loss carry-forward, no other income; intraday trades are not included; not tax advice")
