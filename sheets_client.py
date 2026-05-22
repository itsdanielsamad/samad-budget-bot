"""Google Sheets client — read Vendor Memory + Expenses, append to Spent Bucket."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, date
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


@dataclass
class Transaction:
    date: date
    payer: str
    amount: float
    line_item: str
    category: str
    vendor: str
    status: str


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

    # ---------- Read transactions for a month (digest/reports) ----------
    def get_transactions_for_month(self, year: int, month: int) -> list[Transaction]:
        """Read Spent Bucket and return Confirmed transactions for the given year/month."""
        ws = self._sh.worksheet(config.TAB_SPENT_BUCKET)
        rows = ws.get_all_values()
        out: list[Transaction] = []
        # Header at row 5; data starts row 6 (rows[5])
        for r in rows[5:]:
            if len(r) < 10 or not r[1].strip():
                continue
            try:
                row_year = int(r[8]) if r[8].strip() else 0
                row_month = int(r[9]) if r[9].strip() else 0
            except ValueError:
                continue
            if row_year != year or row_month != month:
                continue
            status = r[11].strip() if len(r) > 11 else "Confirmed"
            if status == "Reversed":
                continue
            ts_raw = r[1].strip()
            ts = None
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
                try:
                    ts = datetime.strptime(ts_raw, fmt)
                    break
                except ValueError:
                    pass
            if ts is None:
                continue
            try:
                amount = _parse_money(r[5])
            except Exception:
                continue
            out.append(Transaction(
                date=ts.date(),
                payer=r[2].strip() if len(r) > 2 else "",
                amount=amount,
                line_item=r[6].strip() if len(r) > 6 else "",
                category=r[7].strip() if len(r) > 7 else "",
                vendor=r[4].strip() if len(r) > 4 else "",
                status=status,
            ))
        return out

    # ---------- Carry Over auto-drain (1st of month) ----------
    def drain_carry_over_balances(self) -> list[tuple[str, float, float]]:
        """For each expense line on the Expenses tab, reduce Carry Over Balance (col F)
        by Monthly Budget (col D), floored at 0. Returns [(line_item, old, new)] for items drained."""
        ws = self._sh.worksheet(config.TAB_EXPENSES)
        rows = ws.get_all_values()
        drained: list[tuple[str, float, float]] = []
        updates: list[dict] = []
        for idx, r in enumerate(rows, start=1):
            if not r or len(r) < 6:
                continue
            item = r[1].strip() if len(r) > 1 else ""
            if not item:
                continue
            # Skip category headers (e.g. "1.  Housing & Utilities")
            if item[0].isdigit() and "." in item[:3]:
                continue
            if (item.startswith("Subtotal") or item.startswith("GRAND")
                    or item.startswith("Annual")):
                continue
            budget = _parse_money(r[3] if len(r) > 3 else "")
            carry = _parse_money(r[5] if len(r) > 5 else "")
            if budget <= 0 or carry <= 0:
                continue
            new_carry = max(0.0, carry - budget)
            if abs(new_carry - carry) > 0.001:
                updates.append({"range": f"F{idx}", "values": [[new_carry]]})
                drained.append((item, carry, new_carry))
        if updates:
            ws.batch_update(updates, value_input_option="USER_ENTERED")
        return drained

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
