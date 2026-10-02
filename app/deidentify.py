"""DICOM 数据集清洗：标准 PHI、私有标签、Overlay/Curve、UID 与日期。

设计要点：
* 标签删除分三层：强制 PHI（Basic Application Confidentiality Profile
  常见字段）、策略 remove_tags、递归私有 creator + 私有数据元素；
* 策略 keep_tags 只对 *非强制* 字段有效——患者身份字段不允许保留；
* Overlay(60xx,3000)/Curve(50xx,3000) 等像素旁路数据一律清除，
  因为遮挡框无法作用于这些可能烧录 PHI 的数据；
* 遍历在 SQ 内递归；UI 元素中“知名 UID”（传输语法、SOP 类等）保留，
  其余按实例/帧/通用 UID 确定性映射；
* DA/DT 元素按 study 统一平移量重写；PN 元素清空而不是删除（保持结构）。
"""

from __future__ import annotations

from pydicom.dataset import Dataset
from pydicom.tag import BaseTag, Tag

from .dates import shift_da, shift_dt
from .issues import IssueList
from .schema import PROTECTED_TAGS
from .uidmap import UidMapper

# --- 标准 PHI 标签（参照 DICOM PS3.15 E.1 Basic Application Confidentiality
#     Profile 的常见集合）。pydicom 字典里不存在的关键字在 build_phi_tag_set
#     中会被跳过。列表本身保持去重、分类注释，方便审计与维护。 ---
PHI_KEYWORDS: list[str] = [
    # —— 患者身份与人口学 ——
    "PatientName", "PatientID", "IssuerOfPatientID",
    "IssuerOfPatientIDQualifiersSequence",
    "PatientIDIssuerIdentificationSequence", "TypeOfPatientID",
    "PatientBirthName", "PatientMotherBirthName",
    "MedicalRecordLocator", "PatientBirthDate", "PatientBirthTime",
    "PatientSexNeutered", "PatientBreedDescription",
    "PatientSpeciesDescription", "PatientBreedCodeSequence",
    "PatientSpeciesCodeSequence", "PatientAnatomicalColorSequence",
    "OtherPatientIDs", "OtherPatientIDsSequence",
    "OtherPatientNames", "OtherPatientNamesSequence",
    "PatientAddress", "PatientCountyResidence", "PatientRegionResidence",
    "PatientTelephoneNumbers", "PatientTelecomInformation",
    "PatientReligiousPreference", "PatientComments",
    "PatientInsurancePlanCodeSequence",
    "PatientPrimaryLanguageCodeSequence",
    "PatientPrimaryLanguageModifierCodeSequence",
    "MilitaryRank", "MilitaryRankCodeSequence",
    "BranchOfService", "BranchOfServiceCodeSequence",
    "Occupation", "SmokingStatus", "PregnancyStatus",
    "LastMenstrualDate", "EthnicGroup", "PatientEthnicGroup",
    # —— 责任方 / 预约 ——
    "ResponsiblePerson", "ResponsiblePersonRole",
    "ResponsibleOrganization", "ReferencedPatientPhotoSequence",
    "PatientPhotoCodeSequence", "InsurancePlanIdentification",
    "MedicalRecordIdentification",
    # —— 检查申请 / 机构 / 人员 ——
    "AccessionNumber", "AccessionNumberCodeSequence",
    "IssuerOfAccessionNumberSequence",
    "InstitutionName", "InstitutionAddress", "InstitutionCodeSequence",
    "InstitutionalDepartmentName",
    "InstitutionalDepartmentTypeCodeSequence",
    "ReferringPhysicianName", "ReferringPhysicianAddress",
    "ReferringPhysicianTelephoneNumbers",
    "ReferringPhysicianIdentificationSequence",
    "PhysiciansOfRecord", "PhysiciansOfRecordIdentificationSequence",
    "NameOfPhysiciansReadingStudy",
    "PhysiciansReadingStudyIdentificationSequence",
    "PerformingPhysicianName", "PerformingPhysicianIdentificationSequence",
    "OperatorsName", "OperatorIdentificationSequence",
    "AdmittingDiagnosesDescription", "AdmittingDate", "AdmittingTime",
    "RequestingPhysician", "RequestingPhysicianIdentificationSequence",
    "RequestingService", "RequestingServiceCodeSequence",
    "RequestAttributesSequence", "ProcedureCodeSequence",
    "StudyID", "StudyDescription", "SeriesDescription", "StudyComments",
    "AdditionalPatientHistory", "PatientState",
    "CurrentPatientLocation", "PatientInstitutionResidence",
    "VisitComments",
    # —— 设备身份（AE Title / 站点名可能回放机构信息） ——
    "StationName", "StationAETitle",
    "DeviceSerialNumber", "DeviceUID", "Manufacturer",
    "ManufacturerDeviceClassUID", "SoftwareVersions",
    # —— 图像 / 内容中的自由文本与人员 ——
    "ImageComments", "UserComments", "TextComments",
    "ContributionDescription", "SamplePosition",
    "DischargeDiagnosisDescription", "DischargeDiagnosisCodeSequence",
    "ServiceEpisodeDescription", "ReasonForStudy", "ReasonForVisit",
    "TreatmentLocation",
    "ContentCreatorName", "ContentCreatorIdentificationSequence",
    "VerifyingObserverName", "VerifyingObserverIdentificationSequence",
    "VerifyingOrganization", "VerifyingDate", "VerifyingTime",
    "AuthorObserverSequence", "ParticipantSequence",
    "ObserverContextSequence", "SubjectRelativePosition",
    "PersonIdentificationCodeSequence", "PersonAddress",
    "PersonTelephoneNumbers", "PersonTelecomInformation", "PersonName",
    "HumanPerformerCodeSequence", "HumanPerformerName",
    "HumanPerformerOrganization",
    # —— 签名 / 审计 / 封装文档 ——
    "DigitalSignaturesSequence", "MAC", "DataSetTrailingPadding",
    "EncryptedAttributesSequence", "OriginalAttributesSequence",
    "ModifiedAttributesSequence",
    "HL7DocumentEffectiveTime", "HL7StructuredDocumentReferenceSequence",
    "MIMETypeOfEncapsulatedDocument",
]

# 整个组强制清空的组号
FORCED_REMOVE_GROUPS = {
    0x0042,  # 临床记录标签组（含老版患者标识）
}

# 这些序列整体删除（内部多为人员/机构结构）
FORCED_REMOVE_SEQUENCE_KEYWORDS = [
    "OriginalAttributesSequence",
    "DigitalSignaturesSequence",
    "EncapsulatedDocumentSequence",
]

# PatientIdentityRemoved 等去标识化声明标签（输出中重新写入）
PATIENT_IDENTITY_REMOVED = Tag(0x00120062)
DEIDENTIFICATION_METHOD = Tag(0x00120063)
DEIDENTIFICATION_METHOD_CODE_SEQ = Tag(0x00120064)


def build_phi_tag_set() -> set[BaseTag]:
    from pydicom.datadict import tag_for_keyword

    tags: set[BaseTag] = set()
    for kw in PHI_KEYWORDS:
        t = tag_for_keyword(kw)
        if t is not None:
            tags.add(Tag(t))
    for kw in FORCED_REMOVE_SEQUENCE_KEYWORDS:
        t = tag_for_keyword(kw)
        if t is not None:
            tags.add(Tag(t))
    # 封装文档（可能含 PDF/CDA 患者信息）
    t = tag_for_keyword("EncapsulatedDocument")
    if t is not None:
        tags.add(Tag(t))
    return tags


# --- 知名 UID：这些是“编码方案”而非实例身份，不参与映射 ---
def _well_known_uids() -> set[str]:
    try:
        from pydicom.uid import UID_dictionary

        # UID_dictionary 的键是注册的 UID 字符串（SOP 类、传输语法等）
        return {str(k) for k in UID_dictionary}
    except Exception:  # pragma: no cover - pydicom 版本兜底
        return set()


WELL_KNOWN_UIDS = _well_known_uids()

# 顶层实例身份 UID 标签 -> 映射类别
_TOP_UID_KINDS = {
    Tag(0x0020000D): "study",   # StudyInstanceUID
    Tag(0x0020000E): "series",  # SeriesInstanceUID
    Tag(0x00080018): "instance",  # SOPInstanceUID
    Tag(0x00200052): "frame",   # FrameOfReferenceUID
}


def _is_overlay_data_tag(tag: BaseTag) -> bool:
    # Overlay data (60xx,3000) 与 Overlay 注释/标签行
    return (tag.group & 0xFF00) == 0x6000 and tag.element in (0x3000, 0x4000)


def _is_curve_data_tag(tag: BaseTag) -> bool:
    return (tag.group & 0xFF00) == 0x5000 and tag.element == 0x3000


def _remove_private(ds: Dataset, removed: dict[str, int]) -> None:
    # 先移除私有 creator（ds.remove_private_tags 会一起清掉数据元素）
    before = len(ds)
    ds.remove_private_tags()
    removed["private_elements"] += before - len(ds)


def _map_ui_value(value: str, mapper: UidMapper) -> str:
    if value in WELL_KNOWN_UIDS:
        return value
    return mapper.map_uid("ui", value)


def _map_dataset_uids(
    ds: Dataset, mapper: UidMapper, *, top: bool, mapped_count: dict[str, int]
) -> None:
    """就地映射数据集中的 UI 元素（SQ 递归）。"""
    for elem in list(ds):
        tag = elem.tag
        if elem.VR == "SQ":
            for item in elem.value or []:
                _map_dataset_uids(item, mapper, top=False,
                                  mapped_count=mapped_count)
            continue
        if elem.VR != "UI" or elem.is_empty:
            continue
        # 引用类 SOP Instance UID（任意嵌套深度）统一走 instance 类别，
        # 保证 ReferencedSOPInstanceUID 与新文件的实例 UID 对得上
        if tag == Tag(0x00081155):
            values = elem.value
            if isinstance(values, (list, tuple)):
                elem.value = [mapper.map_uid("instance", str(v)) for v in values]
                mapped_count["uids"] += len(values)
            else:
                elem.value = mapper.map_uid("instance", str(values))
                mapped_count["uids"] += 1
            continue
        if top and tag in _TOP_UID_KINDS:
            kind = _TOP_UID_KINDS[tag]
            values = elem.value
            if isinstance(values, (list, tuple)):  # VM > 1 的 UI
                elem.value = [mapper.map_uid(kind, str(v)) for v in values]
                mapped_count["uids"] += len(values)
            else:
                elem.value = mapper.map_uid(kind, str(values))
                mapped_count["uids"] += 1
            continue
        # 其余 UI：知名 UID（SOP 类、传输语法、编码方案等）保留，其他伪化
        values = elem.value
        if isinstance(values, (list, tuple)):
            elem.value = [_map_ui_value(str(v), mapper) for v in values]
            mapped_count["uids"] += len(values)
        else:
            elem.value = _map_ui_value(str(values), mapper)
            mapped_count["uids"] += 1


def _blank_pn(ds: Dataset, keep: set[BaseTag], counters: dict[str, int]) -> None:
    """仅清空本层的 PN 元素（SQ 由 _walk_clean 自己递归）。"""
    for elem in list(ds):
        if elem.VR == "PN" and elem.tag not in keep:
            if elem.value not in (None, ""):
                counters["pn_blanked"] += 1
            elem.clear()


def clean_dataset(
    ds: Dataset,
    *,
    mapper: UidMapper,
    keep_tags: set[BaseTag],
    remove_tags: set[BaseTag],
    shift_days: int,
    min_year: int,
    issues: IssueList,
    file_name: str,
    sop_uid: str | None,
    counters: dict[str, int],
) -> None:
    """对一个实例数据集执行完整清洗（不含像素，不含文件元信息）。

    递归只在 *_walk* 中发生一次：删除/PN/日期在同一趟遍历里完成，
    UID 映射随后单独做一趟——绝不能对已经映射过的 UID 再映射，
    否则引用会指向不存在的新 UID。
    """
    phi_tags = build_phi_tag_set()
    protected = set(PROTECTED_TAGS.values())

    # 强制删除：PHI 关键字标签（keep 也无效）、策略删除、整组、overlay/curve
    forced = phi_tags | {PATIENT_IDENTITY_REMOVED, DEIDENTIFICATION_METHOD,
                         DEIDENTIFICATION_METHOD_CODE_SEQ}
    removable = (forced | remove_tags) - protected
    # 患者身份字段不允许 keep
    effective_keep = keep_tags - forced

    _walk_clean(
        ds, removable, effective_keep, shift_days, min_year, issues,
        file_name, sop_uid, counters, top=True,
    )
    # UID 映射单独一趟（顶层 + 全部 SQ）
    _map_dataset_uids(ds, mapper, top=True, mapped_count=counters)


def _walk_clean(
    ds: Dataset,
    removable: set[BaseTag],
    keep: set[BaseTag],
    shift_days: int,
    min_year: int,
    issues: IssueList,
    file_name: str,
    sop_uid: str | None,
    counters: dict[str, int],
    *,
    top: bool,
) -> None:
    # 1) 私有元素
    _remove_private(ds, counters)

    # 2) 删除强制/策略标签、overlay/curve、整组；SQ 继续递归
    for elem in list(ds):
        tag = elem.tag
        if tag in set(PROTECTED_TAGS.values()):
            continue
        if tag in removable or tag.group in FORCED_REMOVE_GROUPS \
                or _is_overlay_data_tag(tag) or _is_curve_data_tag(tag) \
                or tag.is_private:
            counters["elements_removed"] += 1
            del ds[tag]
            continue
        if elem.VR == "SQ":
            for item in elem.value or []:
                _walk_clean(item, removable, keep, shift_days, min_year,
                            issues, file_name, sop_uid, counters, top=False)

    # 3) PN 清空
    _blank_pn(ds, keep, counters)

    # 4) 日期平移（本层元素；已删除的强制 PHI 日期不在其中）
    for elem in list(ds):
        if elem.VR == "SQ":
            continue
        if elem.VR in ("DA", "DT"):
            new = clamped = invalid = None
            if elem.VR == "DA":
                new, clamped, invalid = shift_da(
                    elem.value or "", shift_days, min_year
                )
            else:
                new, clamped, invalid = shift_dt(
                    elem.value or "", shift_days, min_year
                )
            if invalid:
                issues.warning(
                    "date_invalid",
                    f"{elem.VR} 标签 {elem.tag} 的值 {elem.value!r} 无法解析，已清空",
                    file=file_name, sop_instance_uid=sop_uid,
                    tag=str(elem.tag),
                )
                counters["dates_invalid"] += 1
                elem.clear()
            else:
                elem.value = new
                counters["dates_shifted"] += 1
                if clamped:
                    counters["dates_clamped"] += 1


def write_deidentification_declaration(ds: Dataset) -> None:
    """写入去标识化声明（PS3.15 E.1）。"""
    if PATIENT_IDENTITY_REMOVED in ds:
        del ds[PATIENT_IDENTITY_REMOVED]
    if DEIDENTIFICATION_METHOD in ds:
        del ds[DEIDENTIFICATION_METHOD]
    if DEIDENTIFICATION_METHOD_CODE_SEQ in ds:
        del ds[DEIDENTIFICATION_METHOD_CODE_SEQ]
    ds.add_new(PATIENT_IDENTITY_REMOVED, "CS", "YES")
    ds.add_new(DEIDENTIFICATION_METHOD, "LO",
               "DICOM_ANON_API_BASIC_PROFILE_PIXEL_REDACTION")
