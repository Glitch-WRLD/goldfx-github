"""Telegram bot: runs a live scanner loop and drops signals to your chat.

Commands:
  /start      - welcome + current profile
  /status     - scanner health, last scan, today's bias per pair
  /scannow    - force a scan of both pairs now
  /bias       - current higher-TF bias per pair
  /setprofile hi|balanced - switch signal profile
  /history    - last N signals
  /help       - this text
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
from engine import state
from engine.scanner import FVGScanner, format_message
from engine.state import history, touch_last_scan
from strategy.profiles import PROFILES, SYMBOL_RUNTIME

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("goldfx-bot")

try:
    from telegram import Update
    from telegram.ext import (Application, CommandHandler, ContextTypes)
except ImportError as e:  # pragma: no cover
    log.error("python-telegram-bot is required: %s", e)
    raise


def _scanner() -> FVGScanner:
    return FVGScanner(state.get_profile())


async def _broadcast(text: str, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = config.CHAT_ID
    if not chat_id:
        log.warning("CHAT_ID not set - skipping send:\n%s", text)
        return
    await context.bot.send_message(chat_id=chat_id, text=text)


async def run_scan(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Scan both pairs; send found signals (deduped)."""
    scan = FVGScanner(state.get_profile())
    for sym, rt in SYMBOL_RUNTIME.items():
        try:
            sig = scan.scan_symbol(sym, rt["entry_tf"], rt["bias_htf"])
        except Exception as e:  # pragma: no cover
            log.warning("scan error %s: %s", sym, e)
            continue
        if sig is None:
            continue
        payload = {
            "symbol": sym, "dir": "LONG" if sig.direction == 1 else "SHORT",
            "ts": sig.ts.isoformat(), "tf": sig.entry_tf,
            "entry": sig.entry, "sl": sig.stop, "tp": sig.take_profit,
            "rr": sig.rr, "profile": sig.profile,
        }
        if state.mark_sent(sym, sig.ts.isoformat(), payload):
            msg = format_message(sig)
            log.info("signal -> chat: %s", payload)
            await _broadcast(msg, context)
    touch_last_scan()


async def scan_once(context: ContextTypes.DEFAULT_TYPE) -> None:
    await run_scan(context)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    p = state.get_profile()
    await update.message.reply_text(
        "GoldFX Agent online.\n"
        f"Profile: {PROFILES[p]['label']}\n"
        "Add this chat as CHAT_ID in .env and signals will appear here automatically.\n"
        "Use /help for commands.")
    await run_scan(context)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = state._load()
    p = state.get_profile()
    lines = [
        "GoldFX Agent status",
        f"Profile: {PROFILES[p]['label']}",
        f"Last scan: {s.get('last_scan') or 'never'}",
        f"Signals sent: {len(s.get('history', []))}",
        f"Scan interval: {config.SCAN_INTERVAL_SEC}s",
        f"Account balance: ${config.ACCOUNT_BALANCE:,.0f} | risk {config.RISK_PER_TRADE:.1f}%/trade",
    ]
    await update.message.reply_text("\n".join(lines))


async def cmd_scannow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Scanning ...")
    await run_scan(context)
    await update.message.reply_text("Scan done. New signal this bar -> sent above.")


async def cmd_bias(update: Update, context: ContextTypes.DEFAULT_TYPE):
    scan = FVGScanner(state.get_profile())
    lines = []
    for sym, rt in SYMBOL_RUNTIME.items():
        try:
            b = scan.current_bias(sym, rt["entry_tf"], rt["bias_htf"])
        except Exception as e:
            b = f"err({e})"
        lines.append(f"{sym} ({rt['entry_tf']}, bias {rt['bias_htf']}): {b}")
    await update.message.reply_text("\n".join(lines))


async def cmd_setprofile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    arg = (update.message.text.split(maxsplit=1)[1:] or ["hi"])[0].strip().lower()
    if arg not in PROFILES:
        await update.message.reply_text(
            f"Unknown profile '{arg}'. Use /setprofile hi or /setprofile balanced")
        return
    state.set_profile(arg)
    await update.message.reply_text(f"Profile switched to {PROFILES[arg]['label']}.")


async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    hs = history(10)
    if not hs:
        await update.message.reply_text("No signals yet.")
        return
    lines = []
    for h in hs:
        lines.append(
            f"{h['symbol']} {h['dir']} @ {h['ts']} entry={h['entry']} sl={h['sl']} "
            f"tp={h['tp']} rr=1:{h['rr']} ({h['profile']})")
    await update.message.reply_text("\n".join(lines))


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "/start - welcome\n/status - health\n/scannow - force scan\n"
        "/bias - HTF bias per pair\n/setprofile hi|balanced - switch profile\n"
        "/history - last signals\n/help - this text")


def main() -> None:
    if not config.BOT_TOKEN:
        print("ERROR: BOT_TOKEN is not set. Add it to .env (see .env.example).")
        sys.exit(1)

    app = Application.builder().token(config.BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("scannow", cmd_scannow))
    app.add_handler(CommandHandler("bias", cmd_bias))
    app.add_handler(CommandHandler("setprofile", cmd_setprofile))
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_handler(CommandHandler("help", cmd_help))

    app.job_queue.run_repeating(
        scan_once, interval=config.SCAN_INTERVAL_SEC, first=10)
    log.info("GoldFX Agent started. Configured chat: %s", config.CHAT_ID or "(none)")
    app.run_polling()


if __name__ == "__main__":
    main()