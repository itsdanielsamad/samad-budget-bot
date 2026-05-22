"""Google Sheets client — read Vendor Memory + Expenses, append to Spent Bucket."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import gspread
from google.oauth2.service_account import Credentials

import config

log = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]


def _parse_money(s: str) -> float:
    """Extract a number from a cell that might say 'AED 4,000', '(AED 3,000)', '4000', or ''."""
    if not s:
        return 0.0
    s = str(s).strip()
    is_neg = s.startswith("(") and s.endswith(")")
    cleaned = s.replace("AED", "").replace(",", "")
    m = re.search(r"-?\d+(?:\.\d+)?", cleaned)
    if not m:
        return 0.0
    try:
        n = float(m.group())
        return -n if (is_neg and n > 0) else n
    except ValueError:
        return 0.0


@dataclass
class ExpenseLine:
    category: str
    line_item: str
    budget: float


@dataclass
class VendorMemoryEntry:
    vendor: str
    category: str
    line_item: str
    notes: str = ""


class SheetsClient:
    def __init__(self):
        creds = Credentials.from_service_account_info(
            config.GOOGLE_SERVICE_ACCOUNT_INFO, scopes=SCOPES
        )
        self._gc = gspread.authorize(creds)
        self._sh = self._gc.open_by_key(config.SPREADSHEET_ID)

    # ---------- Vendor Memory ----------
    def get_vendor_memory(self) -> list[VendorMemoryEntry]:
        ws = self._sh.worksheet(config.TAB_VENDOR_MEMORY)
        rows = ws.get_all_values()
        entries = []
        for r in rows[5:]:
            if len(r) < 4 or not r[1].strip():
                continue
            entries.append(
                VendorMemoryEntry(
                    vendor=r[1].strip(),
                    category=r[2].strip(),
                    line_item=r[3].strip(),
                    notes=r[4].strip() if len(r) > 4 else "",
                )
            )
        return entries

    # ---------- Expenses (budget reference) ----------
    def get_expense_lines(self) -> list[ExpenseLine]:
        """Return every line item with its category + monthly budget."""
        ws = self._sh.worksheet(config.TAB_EXPENSES)
        rows = ws.get_all_values()
        current_cat = None
        out: list[ExpenseLine] = []
        for r in rows:
            if not r or len(r) < 2:
                continue
            item = r[1].strip() if len(r) > 1 else ""
            # Category header lines like "1.  Housing & Utilities"
            if item and item[0].isdigit() and "." in item[:3]:
                current_cat = item.split(". ", 1)[-1].strip()
                continue
            if (item.startswith("Subtotal") or item.startswith("GRAND")
                    or not item or item.startswith("Annual")):
                continue
            budget_str = r[3] if len(r) > 3 else ""
            budget = _parse_money(budget_str)
            if current_cat:
                out.append(ExpenseLine(category=current_cat, line_item=item, budget=budget))
        return out

    # ---------- Spent Bucket ----------
    def append_transaction(
        self,
        timestamp: datetime,
        payer: str,
        raw_message: str,
        vendor: str,
        amount: float,
        line_item: str,
        category: str,
        confidence: str,
        status: str = "Confirmed",
        notes: str = "",
    ) -> int:
        """Append a transaction row to Spent Bucket. Returns the row number written."""
        ws = self._sh.worksheet(config.TAB_SPENT_BUCKET)
        row = [
            timestamp.strftime("%Y-%m-%d %H:%M:%S"),
            payer,
            raw_message,
            vendor,
            amount,
            line_item,
            category,
            timestamp.year,
            timestamp.month,
            confidence,
            status,
            notes,
        ]
        next_row = self._find_next_spent_bucket_row(ws)
        ws.update(
            range_name=f"B{next_row}:M{next_row}",
            values=[row],
            value_input_option="USER_ENTERED",
        )
        log.info("Appended txn to row %d: %s %.2f → %s", next_row, vendor, amount, line_item)
        return next_row

    def _find_next_spent_bucket_row(self, ws) -> int:
        """Find first empty row at or after row 11 in column B."""
        col_b = ws.col_values(2)
        for i in range(10, len(col_b)):
            if not col_b[i].strip():
                return i + 1
        return len(col_b) + 1

    # ---------- Read live balance for a line item ----------
    def get_remaining_balance(self, category: str, line_item: str) -> Optional[tuple[float, float, float]]:
        """Return (budget, actual_mtd, remaining) for the given line item."""
        ws = self._sh.worksheet(config.TAB_EXPENSES)
        b_col = ws.col_values(2)
        d_col = ws.col_values(4)
        e_col = ws.col_values(5)
        current_cat = None
        for i, item in enumerate(b_col):
            item = item.strip()
            if not item:
                continue
            if item and item[0].isdigit() and "." in item[:3]:
                current_cat = item.split(". ", 1)[-1].strip()
                continue
            if item == line_item and current_cat == category:
                budget = _parse_money(d_col[i] if i < len(d_col) else "")
                actual = _parse_money(e_col[i] if i < len(e_col) else "")
                return budget, actual, budget - actual
        return None
