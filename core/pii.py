"""Mask personal data before it reaches a model or a log (Task 1.5).

Card numbers (checked with the Luhn sum, so order numbers aren't masked), phone numbers,
street addresses and email addresses are replaced by labels such as [PHONE]. A card keeps its
last 4 digits, which support people need and which can't be used to pay.

Rules, not a model: they run on every call for free, and tests pin down what they catch.
"""

import re
from dataclasses import dataclass, field

_CARD = re.compile(r"(?<![\d-])(?:\d[ -]?){12,18}\d(?![\d-])")
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
# A phone number: optional +country, optional (area), then groups of digits. Checked for
# 7-15 digits afterwards, and skipped after words like "order" or "#".
_PHONE = re.compile(
    r"(?<![\w+])(?:\+\d{1,3}[\s.-]?)?(?:\(\d{1,4}\)[\s.-]?)?\d{2,4}(?:[\s.-]?\d{2,4}){1,4}(?!\w)"
)
_NOT_PHONE_BEFORE = re.compile(
    r"(order|invoice|ticket|ref|id|no\.?|number|#|version|v|year|price|\$|usd|eur)\W*$",
    re.IGNORECASE,
)
_DATE_LIKE = re.compile(r"^\d{4}[-/.]\d{1,2}[-/.]\d{1,2}$|^\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}$")
_STREET_SUFFIX = (
    r"street|st|avenue|ave|road|rd|boulevard|blvd|lane|ln|drive|dr|way|court|ct|place|pl|"
    r"square|sq|terrace|parkway|pkwy|highway|hwy|crescent|close|circle|cir"
)
_ADDRESS = re.compile(
    rf"\b\d{{1,6}}[A-Za-z]?\s+(?:[A-Z][\w'.-]*\s+){{0,4}}(?:{_STREET_SUFFIX})\b\.?"
    r"(?:,?\s+(?:apt|apartment|suite|unit|floor|fl)\.?\s*#?\s*\w+)?",
    re.IGNORECASE,
)
_UK_POSTCODE = re.compile(r"\b[A-Z]{1,2}\d[A-Z\d]?\s?\d[A-Z]{2}\b")
_US_ZIP_AFTER_STATE = re.compile(r"\b([A-Z]{2})\s+(\d{5}(?:-\d{4})?)\b")


def luhn_ok(digits: str) -> bool:
    """The Luhn checksum every payment card number passes."""
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


@dataclass
class MaskResult:
    text: str
    counts: dict[str, int] = field(default_factory=dict[str, int])

    @property
    def found(self) -> bool:
        return any(self.counts.values())


def mask_pii(text: str) -> MaskResult:
    counts = {"card": 0, "email": 0, "phone": 0, "address": 0}

    def card(match: re.Match[str]) -> str:
        digits = re.sub(r"\D", "", match.group(0))
        if 13 <= len(digits) <= 19 and luhn_ok(digits):
            counts["card"] += 1
            return f"[CARD ending {digits[-4:]}]"
        return match.group(0)

    def email(match: re.Match[str]) -> str:
        counts["email"] += 1
        return "[EMAIL]"

    def phone(match: re.Match[str]) -> str:
        value = match.group(0)
        digits = re.sub(r"\D", "", value)
        before = text_now[max(0, match.start() - 12) : match.start()]
        if not 7 <= len(digits) <= 15 or _DATE_LIKE.match(value.strip()):
            return value
        if _NOT_PHONE_BEFORE.search(before):
            return value
        # A bare run of digits with no separators and no +: too likely an id, unless long
        if value.isdigit() and len(digits) < 10:
            return value
        counts["phone"] += 1
        return "[PHONE]"

    def address(match: re.Match[str]) -> str:
        counts["address"] += 1
        return "[ADDRESS]"

    text_now = _CARD.sub(card, text)
    text_now = _EMAIL.sub(email, text_now)
    text_now = _ADDRESS.sub(address, text_now)
    text_now = _PHONE.sub(phone, text_now)
    postcode_count = len(_UK_POSTCODE.findall(text_now))
    text_now = _UK_POSTCODE.sub("[POSTCODE]", text_now)
    zip_count = len(_US_ZIP_AFTER_STATE.findall(text_now))
    text_now = _US_ZIP_AFTER_STATE.sub(r"\1 [ZIP]", text_now)
    counts["address"] += postcode_count + zip_count
    return MaskResult(text=text_now, counts={k: v for k, v in counts.items() if v})


def mask(text: str) -> str:
    return mask_pii(text).text
