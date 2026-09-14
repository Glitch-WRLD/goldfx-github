"""Production strategy profiles.

These are the two tuned variants of the FVG-Retest strategy selected by the
backtest sweep + out-of-sample validation (reports/sweep_results.json and
reports/final_validation.json).
"""

HI_PROFILE = {
    "name": "high_winrate",
    "label": "High Win-Rate (~57%)",
    "params": {
        "atr_len": 14, "fvg_lookback": 3, "fvg_max_age": 30, "min_gap": 4,
        "tp_r": 1.0, "bias_htf": "H1", "bias_ema": 50, "long_only": True,
        "bias_htf_by_pair": True, "eur_bias_htf": "H2", "smart_tp": True,
        "min_rr": 0.5, "max_rr": 0.8, "tp_lookback": 60, "confirm": True,
        "min_body_atr": 0.0,
    },
    "backtest": {"n": 1947, "win_rate": 0.577, "profit_factor": 1.36,
                 "total_r": 299.0, "exp_rpermonth": 14.2},
}

BAL_PROFILE = {
    "name": "balanced",
    "label": "Balanced (PF ~1.5)",
    "params": {
        "atr_len": 14, "fvg_lookback": 3, "fvg_max_age": 30, "min_gap": 4,
        "tp_r": 1.5, "bias_htf": "H1", "bias_ema": 50, "long_only": True,
        "bias_htf_by_pair": True, "eur_bias_htf": "H2", "smart_tp": True,
        "min_rr": 0.7, "max_rr": 1.4, "tp_lookback": 60, "confirm": False,
        "min_body_atr": 0.0,
    },
    "backtest": {"n": 2957, "win_rate": 0.496, "profit_factor": 1.48,
                 "total_r": 710.5, "exp_rpermonth": 33.8},
}

# Per-symbol live settings: entry timeframe + higher-TF bias source.
# Chosen on the strongest backtest cells per pair.
SYMBOL_RUNTIME = {
    "XAUUSD": {"entry_tf": "M30", "bias_htf": "H1"},
    "EURUSD": {"entry_tf": "H1", "bias_htf": "H2"},
}

PROFILES = {"hi": HI_PROFILE, "balanced": BAL_PROFILE}


def profile_for(name: str) -> dict:
    return PROFILES.get(name, HI_PROFILE)