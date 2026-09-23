"""Agent : Positions ledger (local JSON).

Tracks every auto-executed setup by its unique ``ref`` so the watcher is
idempotent: a setup is fired exactly once even if state.json is re-read.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

LEDGER_FILE = Path(__file__).resolve().parents[1] / "agent/ledger.json"


class Ledger:
    def __init__(self, path: Path | None = None):
        self.path = path or LEDGER_FILE
        self.path.parent.mkdir(exist_ok=True)
        self.data = self._load()

    def _load(self) -> dict:
        if self.path.exists():
            try:
                return json.loads(self.path.read_text())
            except Exception:
                return {}
        return {}

    def save(self) -> None:
        self.path.write_text(json.dumps(self.data, indent=1, sort_keys=True,
                                        default=str))

    def has(self, ref) -> bool:
        return str(ref) in self.data

    def record_fill(self, ref, fill) -> None:
        self.data[str(ref)] = {
            "ref": ref,
            "symbol": fill.symbol,
            "direction": fill.direction,
            "units": fill.units,
            "fill_price": fill.fill_price,
            "order_id": fill.order_id,
            "broker": fill.broker,
            "ts": fill.ts or dt.datetime.now(dt.timezone.utc).isoformat(),
            "message": fill.message,
        }
        self.save()

    def record_outcome(self, ref, status: str, hit: str = "", exit_price: float = 0.0,
                       pnl_usd: float | None = None, classification: str = "") -> None:
        k = str(ref)
        if k not in self.data:
            self.data[k] = {"ref": ref}
        self.data[k]["status"] = status
        self.data[k]["hit"] = hit
        self.data[k]["exit_price"] = exit_price
        if pnl_usd is not None:
            self.data[k]["pnl_usd"] = round(pnl_usd, 2)
        if classification:
            self.data[k]["classification"] = classification
        self.data[k]["closed_ts"] = dt.datetime.now(dt.timezone.utc).isoformat()
        self.save()


    def open_entries(self) -> dict:
        return {k: v for k, v in self.data.items() if v.get("status", "open") == "open"}

    def today_pnl_usd(self) -> float:
        today = dt.datetime.now(dt.timezone.utc).date().isoformat()
        return sum(
            float(v.get("pnl_usd", 0.0)) for v in self.data.values()
            if v.get("closed_ts", "").startswith(today)
        )

    def consecutive_losses(self) -> int:
        closed = [v for v in self.data.values()
                  if v.get("status") == "closed" and v.get("pnl_usd") is not None]
        n = 0
        for v in sorted(closed, key=lambda x: x.get("closed_ts", ""), reverse=True):
            if v["pnl_usd"] < 0:
                n += 1
            else:
                break
        return n


def load_ledger(path: Path | None = None) -> Ledger:
    return Ledger(path)