# -*- coding: utf-8 -*-
"""Market risk classifier (ChatGPT risk_engine). VIX + interest rate → tier."""
from config import VIX_HIGH, RATE_HIGH


def market_risk(vix, interest_rate):
    """Return 'LOW' / 'MID' / 'HIGH'. None inputs treated as benign."""
    risk = 0
    if vix is not None and vix > VIX_HIGH:
        risk += 1
    if interest_rate is not None and interest_rate > RATE_HIGH:
        risk += 1
    if risk >= 2:
        return "HIGH"
    if risk == 1:
        return "MID"
    return "LOW"
