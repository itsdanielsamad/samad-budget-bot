"""Google Sheets client — read Vendor Memory + Expenses, append to Spent Bucket."""
from __future__ import annotations

import logging
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


@dataclass
class ExpenseLine:
    category: str          # e.g. "Food, Health & Lifestyle"
    line_item: str         # e.g. "Groceries"
    budget: float          # monthly AED


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
        # Headers are at row 5 (rows[4]); data starts row 6 (rows[5])
        entries = []
        for r in rows[5:]:
            # Skip the leading blank column A; vendor is in col B (index 1)
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
        # Row 5 = header; categories announced via merged header rows
        # We walk down and track the current category from "1. ", "2. " etc. headers
        current_cat = None
        out: list[ExpenseLine] = []
        for r in rows:
            if not r or len(r) < 4:
                continue
            item = r[1].strip() if len(r) > 1 else ""
            # Category header lines like "1.  Housing & Utilities"
            if item and item[0].isdigit() and "." in item[:3]:
                # strip leading "1.  " etc.
                current_cat = item.split(". ", 1)[-1].strip()
                continue
            if item.startswith("Subtotal") or item.startswith("GRAND") or not item or item.startswith("Annual"):
                continue
            budget_str = r[3].strip() if len(r) > 3 else ""
            # Skip non-numeric budget cells
            try:
                budget = float(budget_str.replace(",", "")) if budget_str else 0.0
            except ValueError:
                continue
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
        # The Spent Bucket has a header at row 5, examples 6..10, summary at 13+.
        # Find the first empty row after row 10. Simplest: append after the last filled row.
        # gspread's append_row writes after the last row with data — but Spent Bucket has summary block.
        # So we'll find the next empty row in range B11:B200.
        next_row = self._find_next_spent_bucket_row(ws)
        # Column B is timestamp (column index 2)
        ws.update(
            range_name=f"B{next_row}:M{next_row}",
            values=[row],
            value_input_option="USER_ENTERED",
        )
        log.info("Appended txn to row %d: %s %.2f → %s", next_row, vendor, amount, line_item)
        return next_row

    def _find_next_spent_bucket_row(self, ws) -> int:
        """Find first empty row at or after row 11 in column B."""
        col_b = ws.col_values(2)  # column B values, 1-indexed list
        # Start scanning at index 10 (i.e., row 11). The list is 0-indexed.
        for i in range(10, len(col_b)):
            if not col_b[i].strip():
                return i + 1  # convert to 1-indexed
        return len(col_b) + 1

    # ---------- Read live balance for a line item ----------
    def get_remaining_balance(self, category: str, line_item: str) -> Optional[tuple[float, float, float]]:
        """Return (budget, actual_mtd, remaining) for the given line item.
        Reads the Expenses tab where SUMIFS already does the work.
        Returns None if the line item isn't found."""
        ws = self._sh.worksheet(config.TAB_EXPENSES)
        # Read columns B (Item), D (Budget), E (Actual). Adjust as Expenses grows.
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
                try:
                    budget = float(d_col[i].replace("AED", "").replace(",", "").strip() or 0)
                except (ValueError, IndexError):
                    budget = 0.0
                try:
                    actual = float(e_col[i].replace("AED", "").replace(",", "").strip() or 0)
                except (ValueError, IndexError):
                    actual = 0.0
                return budget, actual, budget - actual
        return None
