"""LLM parser — uses Google Gemini (free tier) to parse Telegram messages into structured transactions.

Swap the backend by replacing this module; the bot only depends on `parse_message()` returning a ParsedTxn."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Optional

from google import genai
from google.genai import types

import config

log = logging.getLogger(__name__)

_client = genai.Client(api_key=config.GEMINI_API_KEY)


@dataclass
class ParsedTxn:
    amount: float
    vendor: str
    line_item: str
    category: str
    confidence: str        # "High" | "Medium" | "Low"
    reasoning: str
    suggested_reply: str
    alternatives: list[dict]


PROMPT_TEMPLATE = """You are a household budget assistant for the Samad family (UAE, AED currency).
Your job: parse a short Telegram message describing a spend and map it to a budget line item.

The family's expense line items (Category → Line Item):
{line_item_list}

Vendor memory (high-confidence vendor → line item mapping):
{vendor_memory_list}

Rules:
- Output ONLY valid JSON, no markdown fences, no other text.
- Amount is in AED unless message explicitly says otherwise.
- If the message mentions a vendor in vendor memory, use that mapping with confidence "High".
- If the vendor is unknown but the goods/service is clear (e.g. "spent 50 on coffee"), pick the best line item with "Medium" confidence.
- Confidence "Low" or "Medium" means the bot will ask the user to confirm before logging.
- category MUST be one of: Housing & Utilities, Kids & Education, Transport & Vehicles, Food, Health & Lifestyle.
- line_item MUST exactly match one of the listed items.
- If no amount is extractable, set amount=0 and confidence="Low".
- suggested_reply: a short reply like "Logged AED 250 to Groceries. Remaining this month: AED <REMAINING_PLACEHOLDER>."
- alternatives: up to 3 other plausible {{category, line_item}} pairs (empty list if confidence=High).

User message: {message}

Respond with JSON only:
{{
  "amount": float,
  "vendor": "string",
  "line_item": "string",
  "category": "string",
  "confidence": "High|Medium|Low",
  "reasoning": "1-sentence why",
  "suggested_reply": "string with <REMAINING_PLACEHOLDER>",
  "alternatives": [{{"category": "...", "line_item": "..."}}]
}}
"""


def parse_message(
    message: str,
    line_items: list[tuple[str, str, float]],
    vendor_memory: list[tuple[str, str, str]],
) -> Optional[ParsedTxn]:
    """Return a ParsedTxn or None on failure."""
    line_item_list = "\n".join(
        f"  - {cat} → {item} (AED {budget:.0f}/mo)" for cat, item, budget in line_items
    )
    vendor_memory_list = "\n".join(
        f"  - {v} → {cat} / {li}" for v, cat, li in vendor_memory
    ) or "  (none)"
    prompt = PROMPT_TEMPLATE.format(
        line_item_list=line_item_list,
        vendor_memory_list=vendor_memory_list,
        message=message,
    )
    try:
        resp = _client.models.generate_content(
            model=config.GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.2,
            ),
        )
    except Exception as e:
        log.exception("Gemini API call failed: %s", e)
        return None

    raw = (resp.text or "").strip()
    if not raw:
        log.warning("Gemini returned empty response")
        return None

    # Defensive: strip any code fences if model includes them despite JSON mime type
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:].strip()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("Gemini returned non-JSON: %s", raw[:200])
        return None

    try:
        return ParsedTxn(
            amount=float(data["amount"]),
            vendor=str(data.get("vendor", "")),
            line_item=str(data["line_item"]),
            category=str(data["category"]),
            confidence=str(data["confidence"]),
            reasoning=str(data.get("reasoning", "")),
            suggested_reply=str(data.get("suggested_reply", "")),
            alternatives=list(data.get("alternatives", [])),
        )
    except (KeyError, ValueError, TypeError) as e:
        log.warning("Gemini JSON missing fields: %s — raw: %s", e, raw[:200])
        return None
