import pytest

from arcvoice import contact_id
from arcvoice.contact_id import Category, ContactIdError, Qualifier


def test_known_good_frame_parses():
    frame = contact_id.build("1234", "130", zone="003")
    event = contact_id.parse(frame)
    assert event.account == "1234"
    assert event.category is Category.BURGLARY
    assert event.qualifier is Qualifier.NEW
    assert event.zone == "003"
    assert event.is_actionable


def test_whitespace_is_ignored():
    frame = contact_id.build("5678", "110")
    spaced = f"{frame[:4]} {frame[4:6]} {frame[6]} {frame[7:10]} {frame[10:12]} {frame[12:15]} {frame[15]}"
    assert contact_id.parse(spaced).category is Category.FIRE


@pytest.mark.parametrize("code", ["100", "110", "120", "130", "301", "401", "602", "999"])
def test_checksum_always_valid_for_built_frames(code):
    contact_id.parse(contact_id.build("0042", code))


def test_corrupted_digit_fails_checksum():
    frame = contact_id.build("1234", "130")
    corrupted = frame[:8] + ("4" if frame[8] != "4" else "5") + frame[9:]
    with pytest.raises(ContactIdError, match="checksum"):
        contact_id.parse(corrupted)


@pytest.mark.parametrize("bad", ["", "1234", "12341813001003X5", "1234" * 5])
def test_malformed_frames_rejected(bad):
    with pytest.raises(ContactIdError):
        contact_id.parse(bad)


def test_restores_tests_and_openings_need_no_call():
    assert not contact_id.parse(contact_id.build("1234", "130", qualifier="3")).is_actionable
    assert not contact_id.parse(contact_id.build("1234", "602")).is_actionable
    assert not contact_id.parse(contact_id.build("1234", "401")).is_actionable


def test_unknown_code_falls_back_by_range():
    assert contact_id.parse(contact_id.build("1234", "389")).category is Category.TROUBLE
