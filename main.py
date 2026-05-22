"""@Vault_42_bot — Samad Family budget Telegram bot.

Listens for Telegram messages, parses with Claude API, logs to Google Sheets,
replies with remaining balance for the category."""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta

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
        f"Budget: AED {budget:,.0f}\n"
        f"Spent MTD: AED {actual:,.0f}\n"
        f"Remaining: AED {remaining:,.0f}"
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
    remaining_str = f"AED {res[2]:,.0f}" if res else "unknown"
    reply = parsed.suggested_reply.replace("<REMAINING_PLACEHOLDER>", remaining_str)
    if not reply:
        reply = f"Logged AED {parsed.amount:,.0f} to {parsed.line_item}. Remaining: {remaining_str}."
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
    remaining_str = f"AED {res[2]:,.0f}" if res else "unknown"
    reply = parsed.suggested_reply.replace("<REMAINING_PLACEHOLDER>", remaining_str)
    if not reply:
        reply = f"Logged AED {parsed.amount:,.0f} to {parsed.line_item}. Remaining: {remaining_str}."
    await query.edit_message_text(reply)


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
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text_message))
    log.info("Bot ready. Polling…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
