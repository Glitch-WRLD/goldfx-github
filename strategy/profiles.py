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
        "tp_r": 1.0, "bias_htf": "H1", "bias_ema": 50, "long_only": False,
        "bias_htf_by_pair": True, "eur_bias_htf": "H2", "smart_tp": True,
        "min_rr": 0.5, "max_rr": 0.8, "tp_lookback": 60, "confirm": True,
        "min_body_atr": 0.0, "min_rr_post": 0.50,
        "session_skip_hours": [(13, 15)],
    },
    "backtest": {"n": 1073, "win_rate": 0.566, "profit_factor": 1.30,
                 "total_r": 141.0, "exp_rpermonth": 6.7},
    "backtest_rr050": {"n": 393, "win_rate": 0.547, "profit_factor": 1.21,
                       "total_r": 37.0, "exp_rpermonth": 1.7},
}

BAL_PROFILE = {
    "name": "balanced",
    "label": "Balanced (PF ~1.5)",
    "params": {
        "atr_len": 14, "fvg_lookback": 3, "fvg_max_age": 30, "min_gap": 4,
        "tp_r": 1.5, "bias_htf": "H1", "bias_ema": 50, "long_only": False,
        "bias_htf_by_pair": True, "eur_bias_htf": "H2", "smart_tp": True,
        "min_rr": 0.7, "max_rr": 1.4, "tp_lookback": 60, "confirm": False,
        "min_body_atr": 0.0, "min_rr_post": 0.50,
        "session_skip_hours": [(13, 15)],
    },
    "backtest": {"n": 1654, "win_rate": 0.475, "profit_factor": 1.36,
                 "total_r": 309.1, "exp_rpermonth": 14.7},
    "backtest_rr050": {"n": 635, "win_rate": 0.457, "profit_factor": 1.26,
                       "total_r": 90.8, "exp_rpermonth": 4.3},
}

# Per-symbol live settings: entry timeframe + higher-TF bias source.
# Chosen on the strongest backtest cells per pair.
SYMBOL_RUNTIME = {
    "XAUUSD": {"entry_tf": "M30", "bias_htf": "H1"},
    "AUDUSD": {"entry_tf": "H1", "bias_htf": "H4"},
    "GBPUSD": {"entry_tf": "H1", "bias_htf": "H4"},
}

PROFILES = {"hi": HI_PROFILE, "balanced": BAL_PROFILE}


def profile_for(name: str) -> dict:
    return PROFILES.get(name, HI_PROFILE)