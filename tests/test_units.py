"""单元测试：UID 确定性、日期平移保持跨实例差、标签解析。"""

import datetime as dt

import pytest

from app.dates import shift_da, shift_dt, study_day_shift
from app.schema import parse_tag_spec
from app.uidmap import UidMapper, build_key, validate_uid_root


def test_uid_mapping_deterministic_and_distinct():
    key = build_key(b'{"a":1}')
    m1 = UidMapper(key, "2.25")
    m2 = UidMapper(key, "2.25")
    a = m1.map_uid("instance", "1.2.3.4")
    b = m1.map_uid("instance", "1.2.3.5")
    assert a == m2.map_uid("instance", "1.2.3.4")
    assert a != b
    # 类别之间允许不同
    assert m1.map_uid("study", "1.2.3.4") != a
    assert a.startswith("2.25.")
    assert len(a) <= 64
    assert "." not in a.split(".", 2)[2][:1]  # 数字弧无前导零


def test_uid_root_validation():
    validate_uid_root("2.25")
    validate_uid_root("1.2.826.0.1")
    with pytest.raises(Exception):
        validate_uid_root("not-a-uid")
    with pytest.raises(Exception):
        validate_uid_root("1.02.3")  # 前导零


def test_study_shift_deterministic_and_bounded():
    key = b"k" * 32
    s1 = study_day_shift(key, "1.2.3", 180)
    s2 = study_day_shift(key, "1.2.3", 180)
    assert s1 == s2
    assert -180 <= s1 <= 180
    assert study_day_shift(key, "1.2.3", 0) == 0


def test_cross_instance_time_difference_preserved():
    key = b"k" * 32
    shift = study_day_shift(key, "1.2.3", 365)
    d1 = "20200101"
    d2 = "20200111"
    out1, _, bad1 = shift_da(d1, shift, 1901)
    out2, _, bad2 = shift_da(d2, shift, 1901)
    assert not bad1 and not bad2
    delta_in = (dt.date(2020, 1, 11) - dt.date(2020, 1, 1)).days
    a = dt.date(int(out1[:4]), int(out1[4:6]), int(out1[6:8]))
    b = dt.date(int(out2[:4]), int(out2[4:6]), int(out2[6:8]))
    assert (b - a).days == delta_in


def test_dt_keeps_time_and_timezone():
    out, _, bad = shift_dt("20200115123000.5+0200", 10, 1901)
    assert not bad
    assert out.endswith("123000.5+0200")
    assert out[:8] == "20200125"


def test_shift_clamps_old_dates():
    out, clamped, bad = shift_da("19050101", -3650, 1901)
    assert not bad
    assert clamped
    assert out == "19010101"


def test_invalid_date_is_signalled():
    out, _, bad = shift_da("not-a-d", 1, 1901)
    assert bad and out is None


def test_parse_tag_spec():
    assert parse_tag_spec("PatientName") == parse_tag_spec("(0010,0010)")
    assert parse_tag_spec("0010,0010") == parse_tag_spec("(0010,0010)")
    with pytest.raises(ValueError):
        parse_tag_spec("NoSuchKeyword")
    with pytest.raises(ValueError):
        parse_tag_spec("(zzzz,0010)")
