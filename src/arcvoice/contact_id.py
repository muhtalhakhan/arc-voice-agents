"""Parser for Ademco Contact ID (SIA DC-05) alarm messages.

A Contact ID message is 16 hex digits:

    AAAA MT Q EEE GG CCC S
    |    |  | |   |  |   checksum
    |    |  | |   |  zone or user number
    |    |  | |   group / partition
    |    |  | event code
    |    |  qualifier (1 = new event or opening, 3 = restore or closing, 6 = previously reported)
    |    message type (18 or 98)
    account number

Checksum rule: the sum of all 16 digit values, where "0" counts as 10 and
B-F count as 11-15, must be a multiple of 15.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ContactIdError(ValueError):
    """Raised when a message is not a valid Contact ID frame."""


class Qualifier(StrEnum):
    NEW = "new"
    RESTORE = "restore"
    REPEAT = "repeat"


class Category(StrEnum):
    MEDICAL = "medical"
    FIRE = "fire"
    PANIC = "panic"
    DURESS = "duress"
    BURGLARY = "burglary"
    TAMPER = "tamper"
    TROUBLE = "trouble"
    OPEN_CLOSE = "open_close"
    TEST = "test"
    OTHER = "other"


_QUALIFIERS = {"1": Qualifier.NEW, "3": Qualifier.RESTORE, "6": Qualifier.REPEAT}

# Event code -> (description, category). Not exhaustive; unknown codes fall back by range.
EVENT_CODES: dict[str, tuple[str, Category]] = {
    "100": ("Medical", Category.MEDICAL),
    "101": ("Personal emergency", Category.MEDICAL),
    "110": ("Fire", Category.FIRE),
    "111": ("Smoke", Category.FIRE),
    "114": ("Heat", Category.FIRE),
    "120": ("Panic", Category.PANIC),
    "121": ("Duress", Category.DURESS),
    "122": ("Silent panic", Category.PANIC),
    "130": ("Burglary", Category.BURGLARY),
    "131": ("Perimeter", Category.BURGLARY),
    "132": ("Interior", Category.BURGLARY),
    "134": ("Entry/exit", Category.BURGLARY),
    "137": ("Tamper", Category.TAMPER),
    "144": ("Sensor tamper", Category.TAMPER),
    "301": ("AC power loss", Category.TROUBLE),
    "302": ("Low system battery", Category.TROUBLE),
    "354": ("Failure to communicate", Category.TROUBLE),
    "401": ("Open/close by user", Category.OPEN_CLOSE),
    "602": ("Periodic test report", Category.TEST),
}

_HEX_VALUES = {str(d): d for d in range(1, 10)} | {
    "0": 10,
    "B": 11,
    "C": 12,
    "D": 13,
    "E": 14,
    "F": 15,
}


@dataclass(frozen=True)
class ContactIdEvent:
    account: str
    qualifier: Qualifier
    event_code: str
    description: str
    category: Category
    group: str
    zone: str
    raw: str

    @property
    def is_actionable(self) -> bool:
        """True when an operator (human or AI) needs to act on this signal."""
        if self.qualifier is Qualifier.RESTORE:
            return False
        return self.category not in {Category.TEST, Category.OPEN_CLOSE}


def _digit_value(ch: str) -> int:
    try:
        return _HEX_VALUES[ch]
    except KeyError as exc:
        raise ContactIdError(f"invalid digit {ch!r}") from exc


def checksum_digit(body: str) -> str:
    """Return the checksum digit that completes a 15 digit body."""
    total = sum(_digit_value(c) for c in body.upper())
    needed = (15 - total % 15) % 15 or 15
    return {10: "0", 11: "B", 12: "C", 13: "D", 14: "E", 15: "F"}.get(needed, str(needed))


def _categorise(code: str) -> tuple[str, Category]:
    if code in EVENT_CODES:
        return EVENT_CODES[code]
    ranges = {
        "1": Category.OTHER,
        "3": Category.TROUBLE,
        "4": Category.OPEN_CLOSE,
        "6": Category.TEST,
    }
    return (f"Event {code}", ranges.get(code[0], Category.OTHER))


def parse(raw: str) -> ContactIdEvent:
    frame = "".join(raw.split()).upper()
    if len(frame) != 16:
        raise ContactIdError(f"expected 16 digits, got {len(frame)}")
    total = sum(_digit_value(c) for c in frame)
    if total % 15 != 0:
        raise ContactIdError("checksum mismatch")

    account, mtype, q, code, group, zone = (
        frame[0:4],
        frame[4:6],
        frame[6],
        frame[7:10],
        frame[10:12],
        frame[12:15],
    )
    if mtype not in {"18", "98"}:
        raise ContactIdError(f"unsupported message type {mtype}")
    if q not in _QUALIFIERS:
        raise ContactIdError(f"unknown qualifier {q}")

    description, category = _categorise(code)
    return ContactIdEvent(
        account=account,
        qualifier=_QUALIFIERS[q],
        event_code=code,
        description=description,
        category=category,
        group=group,
        zone=zone,
        raw=frame,
    )


def build(account: str, event_code: str, *, qualifier: str = "1", group: str = "01", zone: str = "001") -> str:
    """Build a valid frame. Handy for tests, simulators and evals."""
    body = f"{account}18{qualifier}{event_code}{group}{zone}"
    return body + checksum_digit(body)
