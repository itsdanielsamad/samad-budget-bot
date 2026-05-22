"""DEPRECATED — see llm_parser.py.

The parser now uses Google Gemini (free tier) instead of Anthropic Claude.
This file is kept only to avoid breaking any old imports.
"""
from llm_parser import parse_message, ParsedTxn  # noqa: F401
