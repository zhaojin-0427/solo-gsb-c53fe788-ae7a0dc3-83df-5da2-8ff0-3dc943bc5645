"""逐项问题（issue）收集模型。

需求要求“缺失引用、非法 VR、压缩像素或策略冲突须逐项报告且不产出半成品”，
因此流水线全程把错误聚合成 Issue 列表；出现任意 error 时丢弃全部暂存
输出，只把审计式的问题清单写进数据库。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

# 稳定的机器可读问题码，方便客户端断言
POLICY_INVALID = "policy_invalid"          # 策略 JSON / 字段非法
POLICY_CONFLICT = "policy_conflict"        # keep_tags 与 remove_tags 冲突等
POLICY_TARGET_MISSING = "policy_target_missing"  # 遮挡规则找不到目标
ZIP_INVALID = "zip_invalid"                # ZIP 打不开 / 路径逃逸
DICOM_READ_ERROR = "dicom_read_error"      # 文件不是合法 DICOM
ILLEGAL_VR = "illegal_vr"                  # 非法 VR / 数据集结构
COMPRESSED_PIXEL = "compressed_pixel"      # 不支持的压缩传输语法
MISSING_REFERENCE = "missing_reference"    # 引用了不存在的 SOP 实例
PIXEL_ERROR = "pixel_error"                # 像素遮挡无法执行
HIERARCHY_ERROR = "hierarchy_error"        # 缺少层级 UID 等
DUPLICATE_INSTANCE = "duplicate_instance"  # 同一 SOP Instance UID 出现多次
DATE_INVALID = "date_invalid"              # 日期字段无法解析（仅警告）


@dataclass
class Issue:
    code: str
    severity: str  # "error" | "warning"
    message: str
    file: str | None = None
    study_uid: str | None = None
    series_uid: str | None = None
    sop_instance_uid: str | None = None
    tag: str | None = None

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass
class IssueList:
    issues: list[Issue] = field(default_factory=list)

    def error(self, code: str, message: str, **kw) -> Issue:
        issue = Issue(code=code, severity="error", message=message, **kw)
        self.issues.append(issue)
        return issue

    def warning(self, code: str, message: str, **kw) -> Issue:
        issue = Issue(code=code, severity="warning", message=message, **kw)
        self.issues.append(issue)
        return issue

    @property
    def has_errors(self) -> bool:
        return any(i.severity == "error" for i in self.issues)

    @property
    def error_count(self) -> int:
        return sum(i.severity == "error" for i in self.issues)

    @property
    def warning_count(self) -> int:
        return sum(i.severity == "warning" for i in self.issues)

    def as_dicts(self) -> list[dict]:
        return [i.to_dict() for i in self.issues]
