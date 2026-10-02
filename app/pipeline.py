"""匿名化作业流水线。

阶段（任何阶段出现 error 都不会产出结果 ZIP——“不产出半成品”）：

1. 解压与 DICOM 解析（路径逃逸、坏文件、非法 VR 逐项报错）；
2. 层级关系建立与重复实例检查（Study/Series/Instance）；
3. 引用完整性检查（ReferencedSOPInstanceUID 必须存在于包内）；
4. 压缩像素检查（仅未压缩传输语法允许，因为要真正覆盖像素）；
5. 策略目标解析（遮挡规则必须命中至少一个实例，帧号越界判错）；
6. 确定性派生：UID 映射、每个 Study 的日期平移量；
7. 清洗 + 像素覆盖 + 重新编码（Explicit VR Little Endian）；
8. 确定性 ZIP 打包 + 审计摘要（含可复验哈希）。
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from pydicom import dcmread
from pydicom.dataelem import RawDataElement
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.tag import BaseTag, Tag
from pydicom.uid import (
    ExplicitVRLittleEndian,
    ImplicitVRLittleEndian,
    UID,
)

from .dates import study_day_shift
from .deidentify import build_phi_tag_set, clean_dataset, write_deidentification_declaration
from .issues import (
    COMPRESSED_PIXEL,
    DICOM_READ_ERROR,
    DUPLICATE_INSTANCE,
    HIERARCHY_ERROR,
    ILLEGAL_VR,
    MISSING_REFERENCE,
    POLICY_CONFLICT,
    POLICY_INVALID,
    POLICY_TARGET_MISSING,
    ZIP_INVALID,
    Issue,
    IssueList,
)
from .pixels import apply_occlusions
from .schema import (
    PROTECTED_TAGS,
    Policy,
    parse_tag_spec,
)
from .uidmap import UidMapper, build_key, validate_uid_root

# 允许的（未压缩）传输语法
UNCOMPRESSED_TS = {
    str(ImplicitVRLittleEndian),
    str(ExplicitVRLittleEndian),
    "1.2.840.10008.1.2.2",  # Explicit VR Big Endian（旧版标准，读入后统一转小端）
}

DEID_CODE = "113100"  # DCM: Basic Application Confidentiality Profile

# 引用 SOP 实例的标签（值是 UI，出现在各类 Referenced... 序列里）
REFERENCED_SOP_INSTANCE_UID = Tag(0x00081155)
REFERENCED_FRAME_NUMBER = Tag(0x00081140)  # 仅用于审计辅助，不强制存在

IMPLEMENTATION_CLASS_UID = UID("1.2.826.0.1.3680043.10.1.1")
IMPLEMENTATION_VERSION_NAME = "ANON_API_1"

ZIP_COMPRESSION = zipfile.ZIP_DEFLATED
# 固定压缩级别，保证可重复构建
ZIP_COMPRESSLEVEL = 6


@dataclass
class Instance:
    member: str                     # 输入 ZIP 内路径
    ds: Dataset
    transfer_syntax: str
    study_uid: str
    series_uid: str
    sop_uid: str
    raw_sha256: str


@dataclass
class JobOutcome:
    status: str                     # "completed" | "failed"
    output_path: Path | None = None
    output_sha256: str | None = None
    output_size: int | None = None
    audit: dict | None = None
    issues: list[Issue] = field(default_factory=list)
    instances_in: int = 0
    instances_out: int = 0
    studies: int = 0
    series: int = 0


# ---------------------------------------------------------------------------
# 策略解析
# ---------------------------------------------------------------------------

def load_policy(policy_bytes: bytes, issues: IssueList) -> Policy | None:
    try:
        obj = json.loads(policy_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        issues.error(POLICY_INVALID, f"策略不是合法 UTF-8 JSON: {exc}")
        return None
    if not isinstance(obj, dict):
        issues.error(POLICY_INVALID, "策略根必须是 JSON 对象")
        return None

    try:
        policy = Policy.model_validate(obj)
    except Exception as exc:
        # Pydantic 错误按字段拆成逐项问题，便于客户端逐项修复
        errors = getattr(exc, "errors", lambda: [])()
        if not errors:
            issues.error(POLICY_INVALID, f"策略校验失败: {exc}")
            return None
        for err in errors:
            loc = ".".join(str(p) for p in err.get("loc", ()))
            issues.error(
                POLICY_INVALID,
                f"策略字段 {loc or '<root>'}: {err.get('msg', '非法值')}"
                f"（输入: {err.get('input')!r:.200}）",
            )
        return None

    if policy.policy_version != "1.0":
        issues.error(
            POLICY_INVALID,
            f"不支持的策略版本 {policy.policy_version!r}，仅支持 '1.0'",
        )
        return None

    try:
        validate_uid_root(policy.uid_root)
    except Exception as exc:
        issues.error(POLICY_INVALID, str(exc))
        return None

    # keep/remove 标签解析
    keep: set[BaseTag] = set()
    remove: set[BaseTag] = set()
    phi_tags = build_phi_tag_set()
    protected = set(PROTECTED_TAGS.values())
    for spec in policy.keep_tags:
        try:
            tag = parse_tag_spec(spec)
        except ValueError as exc:
            issues.error(POLICY_INVALID, str(exc), tag=spec)
            continue
        if tag in protected:
            issues.error(
                POLICY_CONFLICT,
                f"keep_tags 包含受保护标签 {spec!r}，该标签不允许策略干预",
                tag=spec,
            )
            continue
        if tag in phi_tags:
            issues.error(
                POLICY_CONFLICT,
                f"keep_tags 包含强制清除的 PHI 标签 {spec!r}："
                "患者身份/机构/人员标签不允许保留",
                tag=spec,
            )
            continue
        keep.add(tag)
    for spec in policy.remove_tags:
        try:
            tag = parse_tag_spec(spec)
        except ValueError as exc:
            issues.error(POLICY_INVALID, str(exc), tag=spec)
            continue
        if tag in set(PROTECTED_TAGS.values()):
            issues.error(
                POLICY_CONFLICT,
                f"remove_tags 包含受保护标签 {spec!r}，该标签不允许策略干预",
                tag=spec,
            )
            continue
        remove.add(tag)
    overlap = keep & remove
    for tag in sorted(overlap, key=lambda t: (t.group, t.element)):
        issues.error(
            POLICY_CONFLICT,
            f"标签 ({tag.group:04X},{tag.element:04X}) 同时出现在 keep_tags "
            "与 remove_tags 中，策略冲突",
            tag=str(tag),
        )

    # 遮挡规则的静态合法性（目标存在性在解析完实例后再查）
    for i, rule in enumerate(policy.occlusions):
        if rule.series_uid and not rule.study_uid:
            issues.error(
                POLICY_INVALID,
                f"occlusions[{i}] 指定了 series_uid 但缺少 study_uid",
            )
        if rule.sop_instance_uid and not rule.series_uid:
            issues.error(
                POLICY_INVALID,
                f"occlusions[{i}] 指定了 sop_instance_uid 但缺少 series_uid",
            )
        if rule.frames is not None and len(rule.frames) == 0:
            issues.error(
                POLICY_INVALID,
                f"occlusions[{i}].frames 为空数组；省略字段表示全部帧",
            )
        if rule.frames is not None and len(set(rule.frames)) != len(rule.frames):
            issues.warning(
                POLICY_INVALID,
                f"occlusions[{i}].frames 含重复帧号，已按重复处理",
            )

    if issues.has_errors:
        return None
    return policy


# ---------------------------------------------------------------------------
# ZIP 与 DICOM 读取
# ---------------------------------------------------------------------------

def _safe_member_name(name: str) -> bool:
    if not name or name.endswith("/"):
        return False
    if name.startswith("/") or name.startswith("\\"):
        return False
    if ".." in Path(name).parts:
        return False
    # 拒绝 Windows 盘符 / 反斜杠绝对路径
    if "\\" in name:
        return False
    return True


def _classify_read_error(exc: Exception, member: str, issues: IssueList) -> None:
    text = str(exc)
    # 非法 VR：pydicom 在遇到未知/不允许的 VR 时会报含 "VR" 的错误
    if isinstance(exc, NotImplementedError) and "VR" in text:
        issues.error(ILLEGAL_VR, f"非法或不支持的 VR: {text}", file=member)
        return
    if "Unknown Value Representation" in text or "unknown VR" in text.lower():
        issues.error(ILLEGAL_VR, text, file=member)
        return
    issues.error(DICOM_READ_ERROR, f"DICOM 解析失败: {text}", file=member)


def _force_decode_all(ds: Dataset) -> None:
    """强制把所有 RawDataElement 转换（含 SQ 递归）。

    pydicom 对非法 VR 的报错是惰性的：dcmread 不失败，首次访问元素时才
    抛 NotImplementedError。匿名化前必须完整遍历一遍，以便把“非法 VR”
    作为该文件的一项错误报告，而不是在处理中途炸出半成品。
    """

    def visit(dataset: Dataset) -> None:
        for elem in list(dataset):
            if isinstance(elem, RawDataElement):
                _ = dataset[elem.tag]  # 触发转换
                elem = dataset[elem.tag]
            if elem.VR == "SQ":
                for item in elem.value or []:
                    visit(item)

    visit(ds)
    if ds.file_meta is not None:
        for elem in list(ds.file_meta):
            if isinstance(elem, RawDataElement):
                _ = ds.file_meta[elem.tag]


def read_zip_instances(zip_bytes: bytes, issues: IssueList) -> list[Instance]:
    instances: list[Instance] = []
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile as exc:
        issues.error(ZIP_INVALID, f"ZIP 文件无法打开: {exc}")
        return instances
    with zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        for info in infos:
            member = info.filename
            if not _safe_member_name(member):
                issues.error(ZIP_INVALID,
                             f"ZIP 成员路径非法（绝对路径或目录逃逸）: {member!r}",
                             file=member)
                continue
            try:
                raw = zf.read(info)
            except (zipfile.BadZipFile, RuntimeError, OSError) as exc:
                issues.error(ZIP_INVALID, f"成员 {member!r} 读取失败: {exc}",
                             file=member)
                continue
            if not raw:
                issues.error(DICOM_READ_ERROR, "ZIP 成员为空", file=member)
                continue
            # 没有 DICM 标记的文件直接拒绝（避免把 README/杂项吞进去）
            if raw[128:132] != b"DICM":
                issues.error(
                    DICOM_READ_ERROR,
                    "缺少 DICM 前导标记，不是标准 DICOM Part 10 文件",
                    file=member,
                )
                continue
            try:
                ds = dcmread(io.BytesIO(raw), force=False)
                _force_decode_all(ds)
            except Exception as exc:  # pydicom 的各类解析/非法 VR 异常
                _classify_read_error(exc, member, issues)
                continue

            study_uid = getattr(ds, "StudyInstanceUID", None)
            series_uid = getattr(ds, "SeriesInstanceUID", None)
            sop_uid = getattr(ds, "SOPInstanceUID", None)
            missing = [
                name for name, val in (
                    ("StudyInstanceUID", study_uid),
                    ("SeriesInstanceUID", series_uid),
                    ("SOPInstanceUID", sop_uid),
                ) if not val
            ]
            if missing:
                issues.error(
                    HIERARCHY_ERROR,
                    f"实例缺少层级 UID: {', '.join(missing)}",
                    file=member,
                )
                continue

            ts = str(ds.file_meta.TransferSyntaxUID) if ds.file_meta else \
                str(ImplicitVRLittleEndian)
            raw_sha = hashlib.sha256(raw).hexdigest()
            instances.append(Instance(
                member=member, ds=ds, transfer_syntax=ts,
                study_uid=str(study_uid), series_uid=str(series_uid),
                sop_uid=str(sop_uid), raw_sha256=raw_sha,
            ))
    return instances


def check_structure(
    instances: list[Instance], issues: IssueList
) -> dict[str, dict[str, list[Instance]]]:
    """建立层级、查重、压缩传输语法检查、引用完整性检查。"""
    tree: dict[str, dict[str, list[Instance]]] = {}
    seen: dict[str, Instance] = {}
    for inst in instances:
        if inst.sop_uid in seen:
            issues.error(
                DUPLICATE_INSTANCE,
                f"SOP Instance UID {inst.sop_uid} 在 {seen[inst.sop_uid].member!r} "
                f"与 {inst.member!r} 中重复",
                file=inst.member,
                study_uid=inst.study_uid, series_uid=inst.series_uid,
                sop_instance_uid=inst.sop_uid,
            )
            continue
        seen[inst.sop_uid] = inst
        tree.setdefault(inst.study_uid, {}).setdefault(
            inst.series_uid, []
        ).append(inst)

    if issues.has_errors:
        return tree

    all_uids = set(seen)

    for inst in instances:
        if inst.transfer_syntax not in UNCOMPRESSED_TS:
            issues.error(
                COMPRESSED_PIXEL,
                f"传输语法 {inst.transfer_syntax} 为压缩像素，服务只接受未压缩"
                "（Implicit/Explicit VR Little Endian、Explicit VR Big Endian）"
                "实例以保证遮挡真实写回像素",
                file=inst.member,
                study_uid=inst.study_uid, series_uid=inst.series_uid,
                sop_instance_uid=inst.sop_uid,
            )
        _check_references(inst, all_uids, issues)
    return tree


def _iter_ui_references(elem, path: str = ""):
    """递归产出 (UI 元素, 元素所在的序列路径)。"""
    if getattr(elem, "VR", None) == "SQ":
        for idx, item in enumerate(elem.value or []):
            for sub in list(item):
                yield from _iter_ui_references(
                    sub, f"{path}/{elem.tag}[{idx}]"
                )
    elif getattr(elem, "VR", None) == "UI" and elem.tag == \
            REFERENCED_SOP_INSTANCE_UID:
        yield elem, path


def _check_references(inst: Instance, all_uids: set[str],
                      issues: IssueList) -> None:
    for elem in list(inst.ds):
        for ui_elem, path in _iter_ui_references(elem):
            values = ui_elem.value
            if not isinstance(values, (list, tuple)):
                values = [values]
            for value in values:
                ref = str(value)
                if ref and ref not in all_uids:
                    issues.error(
                        MISSING_REFERENCE,
                        f"缺失引用：{path or '(root)'} 中的 "
                        f"ReferencedSOPInstanceUID {ref} 在提交的包内不存在",
                        file=inst.member,
                        study_uid=inst.study_uid,
                        series_uid=inst.series_uid,
                        sop_instance_uid=inst.sop_uid,
                        tag="(0008,1155)",
                    )


# ---------------------------------------------------------------------------
# 策略目标（遮挡）解析
# ---------------------------------------------------------------------------

def resolve_occlusions(
    policy: Policy,
    tree: dict[str, dict[str, list[Instance]]],
    issues: IssueList,
) -> dict[str, list]:
    """返回 sop_uid -> [(Box, frames|None, rule_index)]。"""
    plan: dict[str, list] = {}
    for i, rule in enumerate(policy.occlusions):
        matched: list[Instance] = []
        if rule.study_uid is None:
            for serieses in tree.values():
                for insts in serieses.values():
                    matched.extend(insts)
        elif rule.study_uid not in tree:
            issues.error(
                POLICY_TARGET_MISSING,
                f"occlusions[{i}] 的 study_uid {rule.study_uid!r} 在包内不存在",
            )
            continue
        elif rule.series_uid is None:
            for insts in tree[rule.study_uid].values():
                matched.extend(insts)
        elif rule.series_uid not in tree[rule.study_uid]:
            issues.error(
                POLICY_TARGET_MISSING,
                f"occlusions[{i}] 的 series_uid {rule.series_uid!r} 在该 study 下不存在",
                study_uid=rule.study_uid,
            )
            continue
        elif rule.sop_instance_uid is None:
            matched = list(tree[rule.study_uid][rule.series_uid])
        else:
            hit = [
                inst for inst in tree[rule.study_uid][rule.series_uid]
                if inst.sop_uid == rule.sop_instance_uid
            ]
            if not hit:
                issues.error(
                    POLICY_TARGET_MISSING,
                    f"occlusions[{i}] 的 sop_instance_uid "
                    f"{rule.sop_instance_uid!r} 在该 series 下不存在",
                    study_uid=rule.study_uid, series_uid=rule.series_uid,
                )
                continue
            matched = hit
        for inst in matched:
            plan.setdefault(inst.sop_uid, []).append(
                (rule.box, rule.frames, i)
            )
    return plan


# ---------------------------------------------------------------------------
# 输出：文件元信息、序列化、打包
# ---------------------------------------------------------------------------

def _rebuild_file_meta(ds: Dataset, mapper: UidMapper) -> FileMetaDataset:
    old = ds.file_meta
    new = FileMetaDataset()
    new.MediaStorageSOPClassUID = UID(str(getattr(ds, "SOPClassUID",
                                                  "1.2.840.10008.5.1.4.1.1.77.1.1")))
    new.MediaStorageSOPInstanceUID = UID(mapper.map_uid("instance", ds.SOPInstanceUID))
    new.TransferSyntaxUID = ExplicitVRLittleEndian
    new.ImplementationClassUID = IMPLEMENTATION_CLASS_UID
    new.ImplementationVersionName = IMPLEMENTATION_VERSION_NAME
    if old is not None and "SourceApplicationEntityTitle" in old:
        pass  # 源 AE Title 不复制（可能标识机构）
    return new


def _serialize(ds: Dataset) -> bytes:
    # 文件元信息中的 TransferSyntaxUID 已设为 Explicit VR Little Endian，
    # pydicom 据此决定字节序。enforce_file_format=True 确保写 128 字节
    # 前导 + DICM + 文件元信息。
    buf = io.BytesIO()
    ds.save_as(buf, enforce_file_format=True)
    return buf.getvalue()


def _output_name(inst: Instance, mapper: UidMapper) -> str:
    new_study = mapper.map_uid("study", inst.study_uid)
    new_series = mapper.map_uid("series", inst.series_uid)
    new_sop = mapper.map_uid("instance", inst.sop_uid)
    return f"{new_study}/{new_series}/{new_sop}.dcm"


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def run_job(
    job_id: str,
    zip_bytes: bytes,
    policy_bytes: bytes,
    out_dir: Path,
) -> JobOutcome:
    issues = IssueList()
    policy = load_policy(policy_bytes, issues)
    instances = read_zip_instances(zip_bytes, issues)

    # 层级与引用检查（即便策略解析失败也尽量把包内问题全部报出来）
    tree = check_structure(instances, issues)
    if policy is not None:
        occlusion_plan = resolve_occlusions(policy, tree, issues)
    else:
        occlusion_plan = {}

    input_zip_sha = hashlib.sha256(zip_bytes).hexdigest()
    policy_sha = hashlib.sha256(policy_bytes).hexdigest()

    if issues.has_errors or policy is None:
        return JobOutcome(
            status="failed",
            issues=issues.issues,
            instances_in=len(instances),
            audit=_failure_audit(
                job_id, policy, policy_bytes, policy_sha,
                input_zip_sha, instances, issues,
            ),
        )

    # 派生密钥 / 映射器
    canonical = policy.canonical_json()
    key = build_key(canonical)
    mapper = UidMapper(key, policy.uid_root)

    # 每个 study 的日期平移量
    shifts = {
        study_uid: study_day_shift(
            key, study_uid, policy.dates.jitter_max_days
        ) if policy.dates.shift else 0
        for study_uid in tree
    }

    # 去重后的实例（duplicate 已在 check_structure 报错；此处防御性过滤）
    unique: list[Instance] = []
    seen: set[str] = set()
    for inst in instances:
        if inst.sop_uid in seen:
            continue
        seen.add(inst.sop_uid)
        unique.append(inst)

    keep_tags: set[BaseTag] = set()
    remove_tags: set[BaseTag] = set()
    for spec in policy.keep_tags:
        keep_tags.add(parse_tag_spec(spec))
    for spec in policy.remove_tags:
        remove_tags.add(parse_tag_spec(spec))

    stage = out_dir / f"stage-{job_id}"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)

    totals = {
        "elements_removed": 0,
        "private_elements": 0,
        "pn_blanked": 0,
        "dates_shifted": 0,
        "dates_clamped": 0,
        "dates_invalid": 0,
        "uids": 0,
        "pixels_redacted": 0,
        "occlusion_hits": 0,
    }
    produced: list[tuple[str, bytes, Instance, list]] = []

    try:
        for inst in unique:
            ds = inst.ds
            rules = [
                (box, frames)
                for box, frames, _idx in occlusion_plan.get(inst.sop_uid, [])
            ]
            hits_before = len(issues.issues)
            hits = apply_occlusions(ds, rules, issues, inst.member, inst.sop_uid)
            if len(issues.issues) != hits_before and issues.has_errors:
                continue
            totals["occlusion_hits"] += len(hits)
            totals["pixels_redacted"] += sum(h.pixels_changed for h in hits)

            clean_dataset(
                ds,
                mapper=mapper,
                keep_tags=keep_tags,
                remove_tags=remove_tags,
                shift_days=shifts[inst.study_uid],
                min_year=policy.dates.min_year,
                issues=issues,
                file_name=inst.member,
                sop_uid=inst.sop_uid,
                counters=totals,
            )
            write_deidentification_declaration(ds)
            ds.file_meta = _rebuild_file_meta(ds, mapper)

            out_bytes = _serialize(ds)
            out_name = _output_name(inst, mapper)
            produced.append((out_name, out_bytes, inst, hits))

        if issues.has_errors:
            shutil.rmtree(stage, ignore_errors=True)
            return JobOutcome(
                status="failed",
                issues=issues.issues,
                instances_in=len(instances),
                audit=_failure_audit(
                    job_id, policy, canonical, policy_sha,
                    input_zip_sha, instances, issues,
                ),
            )

        # 审计对象（AUDIT.json 也放进 ZIP，故其内容必须确定）
        audit = _build_audit(
            job_id=job_id,
            policy=policy,
            canonical=canonical,
            policy_sha=policy_sha,
            input_zip_sha=input_zip_sha,
            instances=unique,
            tree=tree,
            mapper=mapper,
            shifts=shifts,
            produced=produced,
            totals=totals,
            issues=issues,
        )
        # 数据库保存的审计外层 envelope：记录原始提交字节哈希（与作业执行
        # 环境相关，不随 ZIP 分发，避免破坏 ZIP 的字节确定性）
        db_audit = dict(audit)
        db_audit["submission"] = {
            "input_zip_sha256": input_zip_sha,
            "policy_submitted_sha256": policy_sha,
        }

        zip_path = _write_zip(stage, audit, produced)
        final_path = out_dir / f"{job_id}.zip"
        if final_path.exists():
            final_path.unlink()
        # 原子落地，避免半成品可见
        os.replace(zip_path, final_path)
        shutil.rmtree(stage, ignore_errors=True)

        output_bytes = final_path.read_bytes()
        output_sha = hashlib.sha256(output_bytes).hexdigest()
        audit["output"] = {
            "artifact": f"jobs/{job_id}/result",
            "sha256": output_sha,
            "size_bytes": len(output_bytes),
            "zip_format": "ZIP deterministic (DEFLATE level=6, fixed timestamps)",
        }
        # 输出哈希写回后，ZIP 内 AUDIT.json 无法自包含最终 ZIP 的哈希；
        # ZIP 内保存除 output 外的全部可复验信息（实例哈希链），
        # 顶层 output 哈希随 API/数据库返回供客户端复验。
        return JobOutcome(
            status="completed",
            output_path=final_path,
            output_sha256=output_sha,
            output_size=len(output_bytes),
            audit=db_audit,
            issues=issues.issues,
            instances_in=len(instances),
            instances_out=len(produced),
            studies=len(tree),
            series=sum(len(s) for s in tree.values()),
        )
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def _write_zip(
    stage: Path, audit: dict, produced: list[tuple[str, bytes, Instance, list]]
) -> Path:
    # 成员顺序确定：DICOM 按输出路径排序，审计文件最后
    ordered = sorted(produced, key=lambda p: p[0])
    zip_path = stage / "out.zip"
    with zipfile.ZipFile(
        zip_path, "w", compression=ZIP_COMPRESSION,
        compresslevel=ZIP_COMPRESSLEVEL,
    ) as zf:
        # 固定时间戳，消除 ZIP 元数据的时间不确定性
        fixed_dt = (1980, 1, 1, 0, 0, 0)
        for name, data, _inst, _hits in ordered:
            info = zipfile.ZipInfo(name, date_time=fixed_dt)
            info.compress_type = ZIP_COMPRESSION
            info.external_attr = (0o644 << 16)
            info.create_system = 3
            zf.writestr(info, data, compress_type=ZIP_COMPRESSION,
                        compresslevel=ZIP_COMPRESSLEVEL)
        audit_bytes = json.dumps(
            audit, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            indent=None,
        ).encode("utf-8")
        info = zipfile.ZipInfo("AUDIT.json", date_time=fixed_dt)
        info.compress_type = ZIP_COMPRESSION
        info.external_attr = (0o644 << 16)
        info.create_system = 3
        zf.writestr(info, audit_bytes, compress_type=ZIP_COMPRESSION,
                    compresslevel=ZIP_COMPRESSLEVEL)
    return zip_path


# ---------------------------------------------------------------------------
# 审计
# ---------------------------------------------------------------------------

def _instance_records(
    produced: list[tuple[str, bytes, Instance, list]], mapper: UidMapper
) -> list[dict]:
    records = []
    for name, out_bytes, inst, hits in sorted(produced, key=lambda p: p[0]):
        records.append({
            "source_member": inst.member,
            "source_sha256": inst.raw_sha256,
            "output_member": name,
            "output_sha256": hashlib.sha256(out_bytes).hexdigest(),
            "study_uid_old": inst.study_uid,
            "study_uid_new": mapper.map_uid("study", inst.study_uid),
            "series_uid_old": inst.series_uid,
            "series_uid_new": mapper.map_uid("series", inst.series_uid),
            "sop_instance_uid_old": inst.sop_uid,
            "sop_instance_uid_new": mapper.map_uid("instance", inst.sop_uid),
            "occlusions": [
                {
                    "frame": h.frame_index,
                    "box": {"x": h.x, "y": h.y, "width": h.width,
                            "height": h.height},
                    "pixels_changed": h.pixels_changed,
                }
                for h in hits
            ],
        })
    return records


def _canonical_json_bytes(obj) -> bytes:
    """审计内部任何嵌套 JSON 都用同一规范化形式，消除字典顺序不确定性。"""
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _build_audit(
    *, job_id, policy, canonical, policy_sha, input_zip_sha,
    instances, tree, mapper, shifts, produced, totals, issues: IssueList,
) -> dict:
    # 输入哈希链：先逐文件 sha256（按 ZIP 成员名排序），再聚合
    members = sorted(((i.member, i.raw_sha256) for i in instances),
                     key=lambda x: x[0])
    chain = hashlib.sha256()
    for member, sha in members:
        chain.update(member.encode("utf-8"))
        chain.update(b":")
        chain.update(bytes.fromhex(sha))
        chain.update(b"\n")
    # 策略审计载荷用规范化 JSON 字符串承载（model_dump 的字典顺序可能随
    # pydantic 版本/插入顺序变化），保证 AUDIT.json 自身字节确定
    policy_payload = json.loads(canonical.decode("utf-8"))
    return {
        "schema": "dicom-anonymization-audit/1.0",
        # 注意：随 ZIP 分发的审计不得包含 job_id / 真实时间戳等每次提交
        # 变化的内容，否则同输入同策略无法字节一致。作业标识只存在于
        # 数据库与 API 层。
        "generated_at_deterministic": "1980-01-01T00:00:00Z",
        "algorithm": {
            "uid": "HMAC-SHA256(key=SHA256(canonical_policy), value)",
            "dates": "per-study deterministic day shift; cross-instance "
                     "deltas preserved",
            "pixel": "uncompressed overwrite via Pillow mask, re-encoded "
                     "Explicit VR Little Endian",
            "encoding": "DICOM Explicit VR Little Endian",
        },
        "policy": {
            "policy_version": policy.policy_version,
            "policy_id": policy.policy_id,
            "canonical_json": _canonical_json_bytes(policy_payload).decode(
                "utf-8"
            ),
            "canonical_sha256": hashlib.sha256(canonical).hexdigest(),
            # 注意：不含 submitted_sha256——它依赖提交时 JSON 的字节排版，
            # 会破坏“同输入同策略字节一致”。原始提交哈希仅保存在数据库
            # audit 的外层 envelope 中。
        },
        "input": {
            "zip_sha256": input_zip_sha,
            "members_sha256_chain": chain.hexdigest(),
            "members": [
                {"member": m, "sha256": s} for m, s in members
            ],
        },
        "structure": {
            "studies": [
                {
                    "old": study,
                    "new": mapper.map_uid("study", study),
                    "date_shift_days": shifts[study],
                    "series": [
                        {
                            "old": series,
                            "new": mapper.map_uid("series", series),
                            "instances": len(insts),
                        }
                        for series, insts in sorted(serieses.items())
                    ],
                }
                for study, serieses in sorted(tree.items())
            ],
        },
        "uid_maps": {
            kind: dict(sorted(table.items()))
            for kind, table in mapper.audit_tables().items()
        },
        "instances": _instance_records(produced, mapper),
        "actions": dict(sorted(totals.items())),
        "issues": issues.as_dicts(),
        "result": "completed",
    }


def _failure_audit(
    job_id, policy, canonical_or_raw, policy_sha, input_zip_sha,
    instances, issues: IssueList,
) -> dict:
    canonical = canonical_or_raw if isinstance(canonical_or_raw, bytes) else b""
    return {
        "schema": "dicom-anonymization-audit/1.0",
        "job_id": job_id,
        "generated_at_deterministic": "1980-01-01T00:00:00Z",
        "policy": {
            "policy_id": getattr(policy, "policy_id", None),
            "canonical_json": canonical.decode("utf-8", errors="replace"),
            "submitted_sha256": policy_sha,
        },
        "input": {"zip_sha256": input_zip_sha},
        "issues": issues.as_dicts(),
        "result": "failed",
        "instances_seen": len(instances),
    }
