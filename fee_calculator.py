"""
fee_calculator.py — Estimates all eBay selling costs for one book.

CURRENT FEE MODEL:
    eBay Final Value Fee:   13.25% of sale price (Books category, 2025)
    Shipping (inbound):     $0.00 — supplier ships directly to your customer
    Shipping (outbound):    $0.00 — you offer free shipping on eBay listings
    Per-order fee:          $0.00 — standard sellers

DROPSHIPPING NOTE:
    In this business model:
    - You list books on eBay before buying from the supplier.
    - When a sale happens, you order from the supplier who ships to the buyer.
    - There is no physical inventory, no packing, and no outbound shipping cost.
    - Your only costs are: supplier fulfillment price + eBay fee.

FUTURE:
    If your supplier begins charging shipping, or if you move to a
    non-free-shipping eBay strategy, update SHIPPING_COST in config.py.
    No other code changes are needed — it flows through automatically.
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config


def estimate_fees(revenue: float, shipping_cost: float = None) -> dict:
    """
    Calculate the full cost of selling one book on eBay.

    Args:
        revenue:       Expected sale price (what buyer pays you)
        shipping_cost: Your outbound shipping cost (defaults to config.SHIPPING_COST = $0)

    Returns dict with fee breakdown.
    """
    if shipping_cost is None:
        shipping_cost = config.SHIPPING_COST

    ebay_fee   = round(revenue * config.EBAY_FEE_RATE, 2)
    per_order  = config.EBAY_PER_ORDER_FEE
    total_fees = round(ebay_fee + shipping_cost + per_order, 2)

    return {
        "ebay_fee":      ebay_fee,
        "shipping_cost": shipping_cost,
        "per_order_fee": per_order,
        "total_fees":    total_fees,
    }


def calculate_profit(cost: float, revenue: float, shipping_cost: float = None) -> dict:
    """
    Calculate net profit and margin for one book.

    Args:
        cost:          Supplier fulfillment price (your COGS per dropship order)
        revenue:       Expected eBay sale price
        shipping_cost: Outbound shipping (default $0 — dropshipping model)

    Returns dict with profit, margin, and full fee breakdown.
    """
    fees   = estimate_fees(revenue, shipping_cost)
    profit = round(revenue - cost - fees["total_fees"], 2)
    margin = round(profit / revenue, 4) if revenue > 0 else 0.0

    return {
        "cogs":       cost,
        "revenue":    revenue,
        "profit":     profit,
        "margin_pct": margin,
        **fees,
    }
