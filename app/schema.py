"""版本化匿名化策略模型（Pydantic v2）。

策略 JSON 示例见 README。所有未声明字段一律拒绝（extra="forbid"），
策略文件的语义校验（受保护标签冲突、遮挡目标等）在 pipeline 的
PolicyError 体系中按 *逐项* 报告，而不是抛出单个 400。
"""

from __future__ import annotations

import json
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field
from pydicom.tag import BaseTag, Tag

from .config import DEFAULT_UID_ROOT, MAX_DATE_JITTER_DAYS

SUPPORTED_POLICY_VERSION = "1.0"

# 无论策略如何配置都不允许 keep/remove 的结构性标签——匿名化必须掌握
# 层级关系与像素描述，否则输出无法保证可解析或 UID 映射不完整。
PROTECTED_TAGS: dict[str, BaseTag] = {
    "StudyInstanceUID": Tag(0x0020000D),
    "SeriesInstanceUID": Tag(0x0020000E),
    "SOPInstanceUID": Tag(0x00080018),
    "SOPClassUID": Tag(0x00080016),
    "TransferSyntaxUID": Tag(0x00020010),
    "Rows": Tag(0x00280010),
    "Columns": Tag(0x00280011),
    "BitsAllocated": Tag(0x00280100),
    "BitsStored": Tag(0x00280101),
    "PixelRepresentation": Tag(0x00280103),
    "SamplesPerPixel": Tag(0x00280002),
    "PhotometricInterpretation": Tag(0x00280004),
    "PixelData": Tag(0x7FE00010),
    "FileMetaInformationGroupLength": Tag(0x00020000),
}


class Box(BaseModel):
    """像素遮挡矩形，坐标为像素列/行（左上角原点，x 向右、y 向下）。"""

    model_config = ConfigDict(extra="forbid")

    x: int = Field(ge=0)
    y: int = Field(ge=0)
    width: int = Field(ge=1)
    height: int = Field(ge=1)
    fill: int = Field(default=0, ge=0)


class OcclusionRule(BaseModel):
    """遮挡规则：study 级 / series 级 / instance 级三选一逐级收窄。"""

    model_config = ConfigDict(extra="forbid")

    study_uid: Optional[str] = None
    series_uid: Optional[str] = None
    sop_instance_uid: Optional[str] = None
    # None 表示作用于匹配实例的所有帧
    frames: Optional[list[int]] = None
    box: Box


class DatePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    shift: bool = True
    # 每个 Study 内统一的确定性伪随机抖动范围（天），0 表示固定平移
    jitter_max_days: int = Field(default=180, ge=0, le=MAX_DATE_JITTER_DAYS)
    # 平移后日期钳制到的最早年份（防止平移到 1900 年之前导致 DA 非法）
    min_year: int = Field(default=1901, ge=1000, le=9998)


class Policy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    policy_version: str
    policy_id: str = Field(min_length=1, max_length=128)
    uid_root: str = DEFAULT_UID_ROOT
    # 标签用 DICOM 关键字（PatientName）或 (gggg,eeee) 两种写法
    keep_tags: list[str] = Field(default_factory=list)
    remove_tags: list[str] = Field(default_factory=list)
    occlusions: list[OcclusionRule] = Field(default_factory=list)
    dates: DatePolicy = Field(default_factory=DatePolicy)

    def canonical_json(self) -> bytes:
        """规范化序列化：排序键、紧凑分隔、UTF-8。

        两份语义相同的策略（仅字段顺序/空白不同）必须产生相同的派生
        密钥与输出哈希，因此所有哈希派生都走这里。
        """
        data = self.model_dump()
        return json.dumps(
            data, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")


def parse_tag_spec(spec: str) -> BaseTag:
    """把策略里的标签写法解析成 pydicom Tag。

    支持：PatientName / PatientID 等关键字，或 "(0010,0010)" / "0010,0010"。
    无法解析时抛 ValueError，由调用方转成逐项策略错误。
    """
    text = spec.strip()
    # 先尝试 (gggg,eeee) 形式
    cleaned = text.strip("()[]").replace(" ", "")
    if "," in cleaned:
        try:
            g_s, e_s = cleaned.split(",", 1)
            return Tag(int(g_s, 16), int(e_s, 16))
        except (ValueError, OverflowError) as exc:
            raise ValueError(f"无法解析标签 {spec!r}: {exc}") from exc
    # 再尝试关键字
    from pydicom.datadict import tag_for_keyword

    tag = tag_for_keyword(text)
    if tag is None:
        raise ValueError(f"未知 DICOM 标签关键字: {spec!r}")
    return Tag(tag)
