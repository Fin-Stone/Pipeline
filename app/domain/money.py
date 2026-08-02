"""Money parsing.

Amounts are integers in minor units. Nothing in this module — or anywhere
downstream of it — is ever a float. See docs/development-rules.md.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

# Currencies whose minor unit is not 1/100. Extend as encountered.
_EXPONENTS = {"JPY": 0, "KRW": 0, "VND": 0, "IDR": 0, "CLP": 0, "ISK": 0, "BHD": 3, "KWD": 3, "OMR": 3, "JOD": 3}
DEFAULT_EXPONENT = 2

# "S$1,234.56", "SGD 1,234.56", "US$5.00", "$5.00"
_SYMBOLS = {"S$": "SGD", "US$": "USD", "A$": "AUD", "HK$": "HKD", "NZ$": "NZD", "RM": "MYR", "£": "GBP", "€": "EUR", "¥": "JPY"}

_AMOUNT_RE = re.compile(
    r"""^\s*
    (?P<sign>[-+])?\s*
    (?P<open_paren>\()?\s*
    (?P<sign2>[-+])?\s*
    (?P<symbol>S\$|US\$|A\$|HK\$|NZ\$|RM|[£€¥$])?\s*
    (?P<digits>\d[\d,\s]*(?:\.\d+)?)\s*
    (?P<close_paren>\))?\s*
    (?P<code>[A-Z]{3})?\s*
    $""",
    re.VERBOSE,
)


class AmountParseError(ValueError):
    """Raised when a string cannot be read as a monetary amount."""


def exponent_for(currency: str | None) -> int:
    return _EXPONENTS.get((currency or "").upper(), DEFAULT_EXPONENT)


def to_minor(value: Decimal, currency: str | None = None) -> int:
    """Convert a Decimal to integer minor units, rejecting sub-unit precision.

    Rounding is refused rather than applied: a statement amount that does not
    land exactly on a minor unit means the parse is wrong, and silently
    rounding it would corrupt the ledger reconciliation by a cent.
    """
    exp = exponent_for(currency)
    scaled = value.scaleb(exp)
    if scaled != scaled.to_integral_value():
        raise AmountParseError(f"{value} has finer precision than {currency or 'the default'} minor units")
    return int(scaled)


def from_minor(minor: int, currency: str | None = None) -> Decimal:
    return Decimal(minor).scaleb(-exponent_for(currency))


def parse_amount(text: str, *, default_currency: str | None = None) -> tuple[int, str | None]:
    """Parse a statement amount into (minor units, currency or None).

    Understands a leading or trailing sign, a currency symbol or ISO code,
    thousands separators, and accounting parentheses for negatives:

        "S$100,000.00" -> (10000000, "SGD")
        "+783.17"      -> (78317, None)
        "1,000.00"     -> (117903, None)
        "25.63 USD"    -> (2563, "USD")
        "(45.00)"      -> (-4500, None)
    """
    if text is None:
        raise AmountParseError("no amount given")
    m = _AMOUNT_RE.match(text.replace(" ", " "))
    if not m:
        raise AmountParseError(f"cannot read {text!r} as an amount")

    digits = re.sub(r"[,\s]", "", m.group("digits"))
    try:
        value = Decimal(digits)
    except InvalidOperation as exc:  # pragma: no cover - guarded by the regex
        raise AmountParseError(f"cannot read {text!r} as an amount") from exc

    negative = bool(m.group("open_paren")) and bool(m.group("close_paren"))
    for group in ("sign", "sign2"):
        if m.group(group) == "-":
            negative = not negative
    if negative:
        value = -value

    currency = m.group("code") or _SYMBOLS.get(m.group("symbol") or "") or default_currency
    if m.group("symbol") == "$" and currency is None:
        currency = default_currency
    return to_minor(value, currency), currency


def is_signed(text: str) -> bool:
    """True when the amount carries an explicit leading '+' or '-'.

    Trust and several other issuers mark credits with a leading '+' and leave
    debits bare, so whether a sign was *written* is itself information the
    adapter needs. Callers must not infer direction from the parsed value.
    """
    return bool(re.match(r"^\s*[-+]", text or ""))
