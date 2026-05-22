"""@Vault_42_bot — Samad Family budget Telegram bot.

Listens for Telegram messages, parses with Claude API, logs to Google Sheets,
replies with remaining balance for the category."""
from __future__ import annotations

import logging
import re
from datetime import datetime, time, timezone, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import config
from llm_parser import parse_message, ParsedTxn
from sheets_client import SheetsClient

logging.basicConfig(level=getattr(logging, config.LOG_LEVEL), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("vault42")

UAE_TZ = timezone(timedelta(hours=4))


def _fmt_aed(n: float) -> str:
    """Format a number for an AED reply — no decimal if whole, two decimals otherwise."""
    try:
        n = float(n)
    except (TypeError, ValueError):
        return str(n)
    if n == int(n):
        return f"{int(n):,}"
    return f"{n:,.2f}"

sheets = SheetsClient()
_vendor_memory_cache: list = []
_line_items_cache: list = []
_cache_loaded_at: datetime | None = None


def _refresh_caches_if_stale(max_age_minutes: int = 60):
    """Re-pull Vendor Memory + Expense line items at most every hour."""
    global _vendor_memory_cache, _line_items_cache, _cache_loaded_at
    now = datetime.now(UAE_TZ)
    if _cache_loaded_at and (now - _cache_loaded_at).total_seconds() < max_age_minutes * 60:
        return
    log.info("Refreshing sheets caches…")
    _vendor_memory_cache = [
        (v.vendor, v.category, v.line_item) for v in sheets.get_vendor_memory()
    ]
    _line_items_cache = [
        (e.category, e.line_item, e.budget) for e in sheets.get_expense_lines()
    ]
    _cache_loaded_at = now
    log.info("Cache: %d vendors, %d line items", len(_vendor_memory_cache), len(_line_items_cache))


def _user_is_allowed(user_id: int) -> bool:
    return not config.ALLOWED_TELEGRAM_USER_IDS or user_id in config.ALLOWED_TELEGRAM_USER_IDS


def _payer_name(user_id: int, username: str | None) -> str:
    return config.PAYER_MAP.get(user_id, username or f"User {user_id}")


# ----- Telegram handlers -----
async def cmd_start(update: Update, _: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hi! Send me what you spent like 'Carrefour 250' and I'll log it.\n"
        "Commands: /balance <line item>, /undo, /help"
    )


async def cmd_help(update: Update, _: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Examples:\n"
        "  Carrefour 250\n"
        "  ADNOC fuel 180\n"
        "  Coffee 30\n"
        "  Amazon 320 — for clothes\n\n"
        "/balance Groceries — see remaining for a line item\n"
        "/undo — mark last logged transaction as Reversed\n"
        "/drain — manually run the Carry Over drain (auto-runs on the 1st)\n"
        "/digest — show this week's digest now (auto-sends every Sunday 20:00)\n"
        "/report [month] — month summary. Try /report, /report 4, /report apr\n"
    )


async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _user_is_allowed(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Usage: /balance Groceries")
        return
    line_item_query = " ".join(context.args).strip()
    _refresh_caches_if_stale()
    # Find best matching line item
    match = None
    for cat, item, _bud in _line_items_cache:
        if item.lower() == line_item_query.lower():
            match = (cat, item)
            break
    if not match:
        await update.message.reply_text(f"Couldn't find line item '{line_item_query}'.")
        return
    cat, item = match
    res = sheets.get_remaining_balance(cat, item)
    if not res:
        await update.message.reply_text(f"No balance info for {item}.")
        return
    budget, actual, remaining = res
    await update.message.reply_text(
        f"{item} ({cat})\n"
        f"Budget: AED {_fmt_aed(budget)}\n"
        f"Spent MTD: AED {_fmt_aed(actual)}\n"
        f"Remaining: AED {_fmt_aed(remaining)}"
    )


async def on_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _user_is_allowed(update.effective_user.id):
        log.warning("Unauthorized user %s", update.effective_user.id)
        return

    msg_text = update.message.text.strip()
    if not msg_text:
        return

    _refresh_caches_if_stale()
    parsed = parse_message(msg_text, _line_items_cache, _vendor_memory_cache)
    if parsed is None:
        await update.message.reply_text("Sorry — I couldn't parse that. Try 'Carrefour 250' or '/help'.")
        return

    if parsed.amount <= 0:
        await update.message.reply_text(
            "I couldn't find an amount in that message. Try 'Vendor 250'."
        )
        return

    # Store the parsed txn in user_data so the confirm-callback can pick it up
    context.user_data["pending_txn"] = parsed
    context.user_data["pending_raw"] = msg_text

    if parsed.confidence == "High":
        await _log_and_reply(update, context, parsed, msg_text)
    else:
        # Ask for confirmation with inline keyboard
        keyboard = [
            [InlineKeyboardButton(f"✅ {parsed.category} → {parsed.line_item}", callback_data="confirm")]
        ]
        for alt in parsed.alternatives[:3]:
            label = f"↪ {alt.get('category', '?')} → {alt.get('line_item', '?')}"
            keyboard.append([InlineKeyboardButton(label, callback_data=f"alt:{alt.get('category')}|{alt.get('line_item')}")])
        keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel")])
        await update.message.reply_text(
            f"I'm {parsed.confidence}-confidence on this:\n"
            f"  Amount: AED {parsed.amount:,.0f}\n"
            f"  Vendor: {parsed.vendor}\n"
            f"  → {parsed.category} / {parsed.line_item}\n\n"
            f"Confirm or pick another:",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    parsed: ParsedTxn = context.user_data.get("pending_txn")
    raw = context.user_data.get("pending_raw", "")
    if not parsed:
        await query.edit_message_text("Session expired. Send the message again.")
        return

    if data == "cancel":
        await query.edit_message_text("Cancelled — nothing logged.")
        return

    if data.startswith("alt:"):
        cat, line = data[4:].split("|", 1)
        parsed.category = cat
        parsed.line_item = line
        parsed.confidence = "High"  # user explicitly chose

    await _log_and_reply_from_callback(query, context, parsed, raw)


async def _log_and_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, parsed: ParsedTxn, raw: str):
    user = update.effective_user
    row = sheets.append_transaction(
        timestamp=datetime.now(UAE_TZ),
        payer=_payer_name(user.id, user.username),
        raw_message=raw,
        vendor=parsed.vendor,
        amount=parsed.amount,
        line_item=parsed.line_item,
        category=parsed.category,
        confidence=parsed.confidence,
        status="Confirmed",
        notes=parsed.reasoning,
    )
    context.user_data["last_logged_row"] = row
    res = sheets.get_remaining_balance(parsed.category, parsed.line_item)
    if res:
        reply = (f"Logged AED {_fmt_aed(parsed.amount)} to {parsed.line_item}. "
                 f"Remaining this month: AED {_fmt_aed(res[2])}.")
    else:
        reply = f"Logged AED {_fmt_aed(parsed.amount)} to {parsed.line_item}."
    await update.message.reply_text(reply)


async def _log_and_reply_from_callback(query, context: ContextTypes.DEFAULT_TYPE, parsed: ParsedTxn, raw: str):
    user = query.from_user
    row = sheets.append_transaction(
        timestamp=datetime.now(UAE_TZ),
        payer=_payer_name(user.id, user.username),
        raw_message=raw,
        vendor=parsed.vendor,
        amount=parsed.amount,
        line_item=parsed.line_item,
        category=parsed.category,
        confidence=parsed.confidence,
        status="Confirmed",
        notes=parsed.reasoning,
    )
    context.user_data["last_logged_row"] = row
    res = sheets.get_remaining_balance(parsed.category, parsed.line_item)
    if res:
        reply = (f"Logged AED {_fmt_aed(parsed.amount)} to {parsed.line_item}. "
                 f"Remaining this month: AED {_fmt_aed(res[2])}.")
    else:
        reply = f"Logged AED {_fmt_aed(parsed.amount)} to {parsed.line_item}."
    await query.edit_message_text(reply)


def _build_weekly_digest() -> str:
    """Assemble the Sunday weekly digest text (plain text, no Markdown)."""
    now = datetime.now(UAE_TZ)
    week_end = now.date()
    week_start = week_end - timedelta(days=6)

    txns = sheets.get_transactions_for_month(now.year, now.month)
    lines = sheets.get_expense_lines()

    budget_by_cat: dict[str, float] = {}
    for ln in lines:
        budget_by_cat[ln.category] = budget_by_cat.get(ln.category, 0.0) + ln.budget

    week_total = 0.0
    week_by_cat: dict[str, float] = {}
    mtd_by_cat: dict[str, float] = {}
    for t in txns:
        mtd_by_cat[t.category] = mtd_by_cat.get(t.category, 0.0) + t.amount
        if week_start <= t.date <= week_end:
            week_total += t.amount
            week_by_cat[t.category] = week_by_cat.get(t.category, 0.0) + t.amount

    parts: list[str] = []
    parts.append(f"📊 Weekly digest — {now.strftime('%a %d %b %Y')}")
    parts.append("")
    parts.append(f"This week ({week_start.strftime('%d %b')}–{week_end.strftime('%d %b')}):")
    parts.append(f"  Total spent: AED {_fmt_aed(week_total)}")
    if week_by_cat:
        for cat, amt in sorted(week_by_cat.items(), key=lambda x: -x[1]):
            parts.append(f"  • {cat}: AED {_fmt_aed(amt)}")
    else:
        parts.append("  (no transactions logged this week)")
    parts.append("")
    parts.append("Month-to-date status:")
    for cat in sorted(budget_by_cat.keys()):
        budget = budget_by_cat[cat]
        actual = mtd_by_cat.get(cat, 0.0)
        pct = (actual / budget * 100) if budget > 0 else 0
        if pct < 80:
            icon = "🟢"
        elif pct < 100:
            icon = "🟡"
        else:
            icon = "🔴"
        parts.append(f"  {icon} {cat}: AED {_fmt_aed(actual)} / {_fmt_aed(budget)} ({pct:.0f}%)")
    mtd_total = sum(mtd_by_cat.values())
    budget_total = sum(budget_by_cat.values())
    parts.append("")
    parts.append(f"MTD total: AED {_fmt_aed(mtd_total)} / AED {_fmt_aed(budget_total)} ({(mtd_total/budget_total*100) if budget_total > 0 else 0:.0f}%)")

    return "\n".join(parts)


def _parse_report_arg(arg: str) -> tuple[int, int] | None:
    """Parse '/report' arg into (year, month). Accepts '4', 'apr', 'April', '2026-04', '04-2026', etc."""
    if not arg:
        return None
    arg = arg.strip().lower()
    now = datetime.now(UAE_TZ)
    month_names = {"jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,
                   "jul":7,"aug":8,"sep":9,"oct":10,"nov":11,"dec":12}
    # YYYY-MM
    m = re.match(r"^(\d{4})[-/](\d{1,2})$", arg)
    if m:
        return int(m.group(1)), int(m.group(2))
    # MM-YYYY
    m = re.match(r"^(\d{1,2})[-/](\d{4})$", arg)
    if m:
        return int(m.group(2)), int(m.group(1))
    # month name
    for name, num in month_names.items():
        if arg.startswith(name):
            return now.year, num
    # plain integer 1-12
    try:
        n = int(arg)
        if 1 <= n <= 12:
            return now.year, n
    except ValueError:
        pass
    return None


def _build_month_report(year: int, month: int) -> str:
    """Comprehensive month report — income, spend by category, top vendors/txns, by payer."""
    month_name = datetime(year, month, 1).strftime("%B %Y")
    txns = sheets.get_transactions_for_month(year, month)
    lines = sheets.get_expense_lines()
    income = sheets.get_monthly_income_budget()

    budget_by_cat: dict[str, float] = {}
    for ln in lines:
        budget_by_cat[ln.category] = budget_by_cat.get(ln.category, 0.0) + ln.budget

    mtd_by_cat: dict[str, float] = {}
    mtd_by_vendor: dict[str, float] = {}
    mtd_by_payer: dict[str, float] = {}
    for t in txns:
        mtd_by_cat[t.category] = mtd_by_cat.get(t.category, 0.0) + t.amount
        if t.vendor:
            mtd_by_vendor[t.vendor] = mtd_by_vendor.get(t.vendor, 0.0) + t.amount
        if t.payer:
            mtd_by_payer[t.payer] = mtd_by_payer.get(t.payer, 0.0) + t.amount

    total_actual = sum(mtd_by_cat.values())
    total_budget = sum(budget_by_cat.values())
    surplus_actual = income - total_actual
    surplus_budget = income - total_budget

    parts: list[str] = []
    parts.append(f"📋 Month report — {month_name}")
    parts.append("")
    parts.append(f"Transactions logged: {len(txns)}")
    parts.append(f"Income (budgeted):   AED {_fmt_aed(income)}")
    parts.append(f"Spent (actual):      AED {_fmt_aed(total_actual)}")
    parts.append(f"Budget (planned):    AED {_fmt_aed(total_budget)}")
    parts.append(f"Surplus (actual):    AED {_fmt_aed(surplus_actual)}")
    parts.append(f"Surplus (budgeted):  AED {_fmt_aed(surplus_budget)}")
    parts.append("")

    parts.append("By category:")
    for cat in sorted(budget_by_cat.keys()):
        budget = budget_by_cat[cat]
        actual = mtd_by_cat.get(cat, 0.0)
        pct = (actual / budget * 100) if budget > 0 else 0
        if pct < 80:
            icon = "🟢"
        elif pct < 100:
            icon = "🟡"
        else:
            icon = "🔴"
        parts.append(f"  {icon} {cat}: AED {_fmt_aed(actual)} / AED {_fmt_aed(budget)} ({pct:.0f}%)")
    parts.append("")

    if mtd_by_vendor:
        parts.append("Top vendors:")
        for vendor, amt in sorted(mtd_by_vendor.items(), key=lambda x: -x[1])[:5]:
            parts.append(f"  • {vendor}: AED {_fmt_aed(amt)}")
        parts.append("")

    if txns:
        top_txns = sorted(txns, key=lambda t: -abs(t.amount))[:5]
        parts.append("Largest transactions:")
        for t in top_txns:
            label = t.vendor or t.line_item
            parts.append(f"  • {t.date.strftime('%d %b')} {label}: AED {_fmt_aed(t.amount)}")
        parts.append("")

    if mtd_by_payer:
        parts.append("By payer:")
        for payer, amt in sorted(mtd_by_payer.items(), key=lambda x: -x[1]):
            parts.append(f"  • {payer}: AED {_fmt_aed(amt)}")

    return "\n".join(parts)


async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Month report — usage: /report, /report 4, /report apr, /report 2026-04."""
    if not _user_is_allowed(update.effective_user.id):
        return
    now = datetime.now(UAE_TZ)
    year, month = now.year, now.month
    if context.args:
        parsed = _parse_report_arg(" ".join(context.args))
        if parsed:
            year, month = parsed
        else:
            await update.message.reply_text(
                "Couldn't parse that month. Try /report, /report 4, /report apr, or /report 2026-04."
            )
            return
    try:
        text = _build_month_report(year, month)
    except Exception as e:
        log.exception("Report failed: %s", e)
        await update.message.reply_text(f"⚠️ Report failed: {e}")
        return
    # Telegram message limit is 4096; chunk if needed
    while text:
        chunk, text = text[:4000], text[4000:]
        await update.message.reply_text(chunk)


async def weekly_digest_job(context: ContextTypes.DEFAULT_TYPE):
    log.info("Running weekly digest…")
    try:
        text = _build_weekly_digest()
    except Exception as e:
        log.exception("Weekly digest failed: %s", e)
        text = f"⚠️ Weekly digest failed: {e}"
    for user_id in config.ALLOWED_TELEGRAM_USER_IDS:
        try:
            await context.bot.send_message(chat_id=user_id, text=text)
        except Exception as e:
            log.warning("Couldn't send digest to %d: %s", user_id, e)


async def cmd_digest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manual trigger of the weekly digest."""
    if not _user_is_allowed(update.effective_user.id):
        return
    try:
        text = _build_weekly_digest()
    except Exception as e:
        log.exception("Manual digest failed: %s", e)
        await update.message.reply_text(f"⚠️ Digest failed: {e}")
        return
    await update.message.reply_text(text)


async def monthly_drain_job(context: ContextTypes.DEFAULT_TYPE):
    """Runs on the 1st of each month at 00:05 UAE. Decrements Carry Over Balance values
    by the monthly budget for each line item, then notifies allowlisted users."""
    log.info("Running monthly Carry Over drain…")
    try:
        drained = sheets.drain_carry_over_balances()
        log.info("Drained %d items", len(drained))
        if drained:
            lines = [f"• {item}: AED {_fmt_aed(old)} → AED {_fmt_aed(new)}" for item, old, new in drained]
            summary = "Monthly Carry Over drain (1st of month):\n\n" + "\n".join(lines)
        else:
            summary = "Monthly check: no Carry Over balances needed draining."
    except Exception as e:
        log.exception("Monthly drain failed: %s", e)
        summary = f"⚠️ Monthly drain failed: {e}"
    for user_id in config.ALLOWED_TELEGRAM_USER_IDS:
        try:
            await context.bot.send_message(chat_id=user_id, text=summary)
        except Exception as e:
            log.warning("Couldn't notify %d: %s", user_id, e)


async def cmd_drain(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manual trigger of the carry-over drain. For testing — auto-runs on the 1st anyway."""
    if not _user_is_allowed(update.effective_user.id):
        return
    await update.message.reply_text("Running Carry Over drain now…")
    try:
        drained = sheets.drain_carry_over_balances()
    except Exception as e:
        log.exception("Manual drain failed: %s", e)
        await update.message.reply_text(f"⚠️ Drain failed: {e}")
        return
    if drained:
        lines = [f"• {item}: AED {_fmt_aed(old)} → AED {_fmt_aed(new)}" for item, old, new in drained]
        await update.message.reply_text("Drained:\n\n" + "\n".join(lines))
    else:
        await update.message.reply_text("Nothing to drain — no positive Carry Over balances.")


async def cmd_undo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    row = context.user_data.get("last_logged_row")
    if not row:
        await update.message.reply_text("Nothing to undo in this session.")
        return
    # Mark the row's Status as Reversed (column L = 12) and Amount = 0 to exclude from SUMIFS
    ws = sheets._sh.worksheet(config.TAB_SPENT_BUCKET)
    ws.update_acell(f"F{row}", 0)
    ws.update_acell(f"L{row}", "Reversed")
    context.user_data["last_logged_row"] = None
    await update.message.reply_text(f"Reversed row {row}. Amount set to 0 and Status=Reversed.")


def main():
    log.info("Starting Vault_42_bot…")
    _refresh_caches_if_stale()
    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("balance", cmd_balance))
    app.add_handler(CommandHandler("undo", cmd_undo))
    app.add_handler(CommandHandler("drain", cmd_drain))
    app.add_handler(CommandHandler("digest", cmd_digest))
    app.add_handler(CommandHandler("report", cmd_report))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text_message))

    # Schedule Carry Over auto-drain for the 1st of every month at 00:05 UAE
    if app.job_queue:
        app.job_queue.run_monthly(
            callback=monthly_drain_job,
            when=time(hour=0, minute=5, tzinfo=UAE_TZ),
            day=1,
        )
        log.info("Scheduled monthly Carry Over drain for day=1 at 00:05 UAE")
        # Weekly digest every Sunday at 20:00 UAE (Python weekday: Mon=0 … Sun=6)
        app.job_queue.run_daily(
            callback=weekly_digest_job,
            time=time(hour=20, minute=0, tzinfo=UAE_TZ),
            days=(6,),
        )
        log.info("Scheduled weekly digest for Sunday 20:00 UAE")
    else:
        log.warning("Job queue not available — scheduled jobs will not auto-run")

    log.info("Bot ready. Polling…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
