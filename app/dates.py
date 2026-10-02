"""确定性日期重写。

规则：
* 每个 Study 内所有 DA/DT 日期使用同一个平移量（天数），保持跨实例
  时间差（instance 之间的先后与间隔不变）；
* 平移量由 HMAC(策略密钥, study 原始 UID) 确定性派生，范围
  [-jitter_max_days, +jitter_max_days]，同输入永远同结果；
* DA 保持 YYYYMMDD；DT 的时间部分、小数秒、时区后缀原样保留；
* 平移后早于 min_year 的日期钳制到 min_year-01-01（审计中计数）；
* 无法解析的日期不静默改动：清空值并登记 date_invalid 警告。

时间字段（TM）不动。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import re

# DA: YYYYMMDD；兼容老设备的 YYYY.MM.DD / MM/DD/YYYY 不做猜测，
# 只接受标准格式（遇非标准走 invalid 路径）。
DA_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})$")
# DT: YYYYMMDDHHMMSS[.fraction][+HHMM|-HHMM]，时间尾部逐段可选
DT_RE = re.compile(
    r"^(?P<date>\d{4}\d{2}\d{2})"
    r"(?P<hms>\d{2}(?:\d{2}(?:\d{2})?)?)?"
    r"(?P<frac>\.\d{1,6})?"
    r"(?P<tz>[+-]\d{4})?$"
)


def study_day_shift(key: bytes, study_uid: str, jitter_max: int) -> int:
    """返回该 study 的确定性平移天数，范围 [-jitter_max, +jitter_max]。"""
    if jitter_max == 0:
        return 0
    digest = hmac.new(
        key, b"date-shift\x00" + study_uid.encode("utf-8"), hashlib.sha256
    ).digest()
    n = int.from_bytes(digest[:8], "big")
    span = 2 * jitter_max + 1
    return (n % span) - jitter_max


def _clamp(date: dt.date, min_year: int) -> dt.date:
    floor = dt.date(min_year, 1, 1)
    if date < floor:
        return floor
    return date


def shift_da(value: str, shift_days: int, min_year: int) -> tuple[str | None, bool, bool]:
    """返回 (新值或 None, 是否钳制, 是否非法)。空字符串原样返回。"""
    if value is None or value == "":
        return value if value is not None else "", False, False
    m = DA_RE.match(value.strip())
    if not m:
        return None, False, True
    try:
        d = dt.date(int(m[1]), int(m[2]), int(m[3]))
    except ValueError:
        return None, False, True
    shifted = d + dt.timedelta(days=shift_days)
    clamped = shifted < dt.date(min_year, 1, 1)
    shifted = _clamp(shifted, min_year)
    return shifted.strftime("%Y%m%d"), clamped, False


def shift_dt(value: str, shift_days: int, min_year: int) -> tuple[str | None, bool, bool]:
    if value is None or value == "":
        return value if value is not None else "", False, False
    m = DT_RE.match(value.strip())
    if not m:
        return None, False, True
    date_part = m.group("date")
    try:
        d = dt.date(int(date_part[0:4]), int(date_part[4:6]), int(date_part[6:8]))
    except ValueError:
        return None, False, True
    shifted = d + dt.timedelta(days=shift_days)
    clamped = shifted < dt.date(min_year, 1, 1)
    shifted = _clamp(shifted, min_year)
    rest = (m.group("hms") or "") + (m.group("frac") or "") + (m.group("tz") or "")
    return shifted.strftime("%Y%m%d") + rest, clamped, False
