"""端到端 API 测试。"""

import io
import zipfile

import numpy as np
from pydicom import dcmread
from pydicom.uid import JPEGBaseline8Bit

from conftest import (
    add_private,
    add_reference,
    make_instance,
    make_policy,
    make_zip,
    poll_job,
    read_output_by_old_uid,
    submit,
)

STUDY = "1.2.3.400"
S1 = "1.2.3.400.1"
S2 = "1.2.3.400.2"
I1 = "1.2.3.400.1.1"
I2 = "1.2.3.400.1.2"
I3 = "1.2.3.400.2.1"


def _multi_series_zip():
    pixel1 = np.arange(32 * 64, dtype=np.uint8).reshape(32, 64)
    pixel2 = np.full((32, 64), 120, dtype=np.uint8)
    pixel3 = np.zeros((32, 64), dtype=np.uint8)
    a = make_instance(STUDY, S1, I1, pixel=pixel1)
    b = make_instance(STUDY, S1, I2, pixel=pixel2,
                      extra_fn=add_private)
    c = make_instance(STUDY, S2, I3, pixel=pixel3)
    return make_zip({
        "dcm/a.dcm": a,
        "dcm/sub/b.dcm": b,
        "c.dcm": c,
    })


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["supported_policy_version"] == "1.0"


def test_happy_path_deterministic_and_redacted(client):
    policy = make_policy(occlusions=[
        {"study_uid": STUDY, "series_uid": S1,
         "box": {"x": 2, "y": 3, "width": 4, "height": 5, "fill": 0}},
    ])
    z = _multi_series_zip()

    body = submit(client, z, policy)
    job = poll_job(client, body["job_id"])
    assert job["status"] == "completed", job["audit"]["issues"]
    assert job["counts"]["instances_in"] == 3
    assert job["counts"]["instances_out"] == 3
    assert job["counts"]["studies"] == 1
    assert job["counts"]["series"] == 2

    result = client.get(f"/jobs/{body['job_id']}/result")
    assert result.status_code == 200
    out_zip = result.content
    # 下载内容哈希必须等于审计公布的哈希（可复验）
    import hashlib

    assert hashlib.sha256(out_zip).hexdigest() == job["hashes"]["output_sha256"]
    assert result.headers["X-Content-SHA256"] == job["hashes"]["output_sha256"]

    audit = job["audit"]
    with zipfile.ZipFile(io.BytesIO(out_zip)) as zf:
        names = zf.namelist()
        assert "AUDIT.json" in names
        dicom_names = [n for n in names if n.endswith(".dcm")]
        assert len(dicom_names) == 3
        # 输出路径采用新 UID 层级
        ds1 = read_output_by_old_uid(zf, audit, I1)
        ds2 = read_output_by_old_uid(zf, audit, I2)
        ds3 = read_output_by_old_uid(zf, audit, I3)

    # PHI 已清除（强制删除的标签直接缺失，其余 PN 清空为空元素）
    assert not hasattr(ds1, "PatientName") or str(ds1.PatientName) == ""
    assert not hasattr(ds1, "PatientID") or str(ds1.PatientID) == ""
    assert not hasattr(ds1, "InstitutionName")
    assert not hasattr(ds1, "AccessionNumber")
    assert not hasattr(ds1, "ImageComments")
    assert ds1.PatientSex == "M"  # keep_tags 生效
    assert ds1.PatientIdentityRemoved == "YES"

    # 私有标签消失
    private_tags = [e.tag for e in ds2 if e.tag.is_private]
    assert private_tags == []
    # 源 AE Title 不复制
    assert "SourceApplicationEntityTitle" not in ds1.file_meta

    # 层级 UID 已映射且与审计一致
    assert str(ds1.StudyInstanceUID) == audit["uid_maps"]["study"][STUDY]
    assert str(ds1.SeriesInstanceUID) == audit["uid_maps"]["series"][S1]
    assert str(ds1.SOPInstanceUID) == audit["uid_maps"]["instance"][I1]
    assert str(ds1.file_meta.MediaStorageSOPInstanceUID) == \
        audit["uid_maps"]["instance"][I1]
    assert str(ds1.file_meta.TransferSyntaxUID) == "1.2.840.10008.1.2.1"

    # 遮挡真正落到像素：框内为 fill=0，框外保持原值
    arr = ds1.pixel_array
    assert arr[3:8, 2:6].max() == 0
    assert arr[3:8, 2:6].min() == 0
    assert arr[10, 10] == ((10 * 64 + 10) % 256)  # 未遮挡像素保持（uint8 回绕）

    # I3 没有遮挡，像素不变
    assert ds3.pixel_array.max() == 0


def test_byte_identical_for_same_input_and_policy(client):
    policy = make_policy()
    z = _multi_series_zip()
    j1 = poll_job(client, submit(client, z, policy)["job_id"])
    # 字段顺序/空白变化的等价策略 JSON
    policy_reordered = b'{\n  "dates": {"min_year": 1901, "jitter_max_days": 180, "shift": true},\n' \
                       b'  "policy_id": "test-policy-1",\n  "policy_version": "1.0",\n' \
                       b'  "remove_tags": [],\n  "keep_tags": ["PatientSex"],\n  "occlusions": []\n}'
    j2 = poll_job(client, submit(client, z, policy_reordered)["job_id"])
    assert j1["status"] == "completed" and j2["status"] == "completed"
    assert j1["hashes"]["output_sha256"] == j2["hashes"]["output_sha256"]

    r1 = client.get(f"/jobs/{j1['job_id']}/result").content
    r2 = client.get(f"/jobs/{j2['job_id']}/result").content
    assert r1 == r2


def test_missing_reference_reported_and_no_output(client):
    def with_ref(ds):
        add_reference(ds, "1.2.3.DOES.NOT.EXIST")

    a = make_instance(STUDY, S1, I1, extra_fn=with_ref)
    z = make_zip({"a.dcm": a})
    job = poll_job(client, submit(client, z, make_policy())["job_id"])
    assert job["status"] == "failed"
    codes = [i["code"] for i in job["audit"]["issues"]]
    assert "missing_reference" in codes
    assert any("1.2.3.DOES.NOT.EXIST" in i["message"]
               for i in job["audit"]["issues"])
    r = client.get(f"/jobs/{job['job_id']}/result")
    assert r.status_code == 409


def test_compressed_pixel_rejected(client):
    a = make_instance(STUDY, S1, I1, transfer_syntax=JPEGBaseline8Bit)
    z = make_zip({"a.dcm": a})
    job = poll_job(client, submit(client, z, make_policy())["job_id"])
    assert job["status"] == "failed"
    codes = [i["code"] for i in job["audit"]["issues"]]
    assert "compressed_pixel" in codes


def test_policy_conflict_keep_and_remove(client):
    policy = make_policy(
        keep_tags=["Modality", "PatientSex"],
        remove_tags=["(0008,0060)"],  # Modality
    )
    job = poll_job(client, submit(client, _multi_series_zip(), policy)["job_id"])
    assert job["status"] == "failed"
    issues = job["audit"]["issues"]
    assert any(i["code"] == "policy_conflict"
               and i.get("tag", "").lower().replace(" ", "") in (
                   "modality", "(0008,0060)", "0008,0060")
               for i in issues)


def test_keep_forced_phi_rejected(client):
    policy = make_policy(keep_tags=["PatientID"])
    job = poll_job(client, submit(client, _multi_series_zip(), policy)["job_id"])
    assert job["status"] == "failed"
    assert any(i["code"] == "policy_conflict" and "PatientID" in i["message"]
               for i in job["audit"]["issues"])


def test_protected_tag_conflict(client):
    policy = make_policy(remove_tags=["SOPInstanceUID"])
    job = poll_job(client, submit(client, _multi_series_zip(), policy)["job_id"])
    assert job["status"] == "failed"
    assert any(i["code"] == "policy_conflict" for i in job["audit"]["issues"])


def test_unsupported_policy_version(client):
    policy = make_policy(policy_version="9.9")
    job = poll_job(client, submit(client, _multi_series_zip(), policy)["job_id"])
    assert job["status"] == "failed"
    assert any(i["code"] == "policy_invalid"
               for i in job["audit"]["issues"])


def test_illegal_vr_reported(client):
    good = make_instance(STUDY, S1, I1)
    # 把 PatientName 元素的 VR 字节改成非法的 "XX"
    assert good[0x80:0x84] == b"DICM"
    idx = good.find(b"PN")
    corrupted = good[:idx] + b"XX" + good[idx + 2:]
    z = make_zip({"a.dcm": corrupted, "b.dcm": make_instance(STUDY, S1, I2)})
    job = poll_job(client, submit(client, z, make_policy())["job_id"])
    assert job["status"] == "failed"
    assert any(i["code"] == "illegal_vr" and i["file"] == "a.dcm"
               for i in job["audit"]["issues"])


def test_duplicate_sop_instance_rejected(client):
    z = make_zip({
        "a.dcm": make_instance(STUDY, S1, I1),
        "b.dcm": make_instance(STUDY, S2, I1),
    })
    job = poll_job(client, submit(client, z, make_policy())["job_id"])
    assert job["status"] == "failed"
    assert any(i["code"] == "duplicate_instance"
               for i in job["audit"]["issues"])


def test_occlusion_target_missing(client):
    policy = make_policy(occlusions=[
        {"study_uid": "9.9.9.9",
         "box": {"x": 0, "y": 0, "width": 2, "height": 2}},
    ])
    job = poll_job(client, submit(client, _multi_series_zip(), policy)["job_id"])
    assert job["status"] == "failed"
    assert any(i["code"] == "policy_target_missing"
               for i in job["audit"]["issues"])


def test_occlusion_frame_out_of_range(client):
    policy = make_policy(occlusions=[
        {"study_uid": STUDY, "series_uid": S1, "sop_instance_uid": I1,
         "frames": [5],
         "box": {"x": 0, "y": 0, "width": 2, "height": 2}},
    ])
    job = poll_job(client, submit(client, _multi_series_zip(), policy)["job_id"])
    assert job["status"] == "failed"
    assert any(i["code"] == "pixel_error" and "帧号越界" in i["message"]
               for i in job["audit"]["issues"])


def test_non_dicom_member_rejected(client):
    z = make_zip({"a.dcm": make_instance(STUDY, S1, I1),
                  "notes.txt": b"hello world"})
    job = poll_job(client, submit(client, z, make_policy())["job_id"])
    assert job["status"] == "failed"
    assert any(i["file"] == "notes.txt" and i["code"] == "dicom_read_error"
               for i in job["audit"]["issues"])


def test_zip_path_traversal_rejected(client):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../evil.dcm", make_instance(STUDY, S1, I1))
    job = poll_job(client, submit(client, buf.getvalue(), make_policy())["job_id"])
    assert job["status"] == "failed"
    assert any(i["code"] == "zip_invalid"
               for i in job["audit"]["issues"])


def test_cross_reference_remapped_consistently(client):
    """实例 2 引用实例 1：输出中的引用必须指向实例 1 的新 UID。"""
    def ref_i1(ds):
        add_reference(ds, I1)

    a = make_instance(STUDY, S1, I1)
    b = make_instance(STUDY, S1, I2, extra_fn=ref_i1)
    z = make_zip({"a.dcm": a, "b.dcm": b})
    job = poll_job(client, submit(client, z, make_policy())["job_id"])
    assert job["status"] == "completed", job["audit"]["issues"]
    out = client.get(f"/jobs/{job['job_id']}/result").content
    with zipfile.ZipFile(io.BytesIO(out)) as zf:
        ds2 = read_output_by_old_uid(zf, job["audit"], I2)
    evidence = ds2.CurrentRequestedProcedureEvidenceSequence[0]
    ref = evidence.ReferencedSeriesSequence[0].ReferencedSOPSequence[0]
    assert str(ref.ReferencedSOPInstanceUID) == \
        job["audit"]["uid_maps"]["instance"][I1]


def test_dates_shifted_within_study_and_preserved_time(client):
    job = poll_job(client, submit(client, _multi_series_zip(), make_policy())["job_id"])
    assert job["status"] == "completed", job["audit"]["issues"]
    out = client.get(f"/jobs/{job['job_id']}/result").content
    shift_days = job["audit"]["structure"]["studies"][0]["date_shift_days"]
    assert shift_days != 0  # 180 天抖动下几乎必然非 0
    with zipfile.ZipFile(io.BytesIO(out)) as zf:
        ds = dcmread(io.BytesIO(zf.read(
            next(n for n in zf.namelist() if n.endswith(".dcm"))
        )))
    # StudyDate 被平移
    assert str(ds.StudyDate) != "20200115"
    # AcquisitionDateTime 保留时间部分
    assert str(ds.AcquisitionDateTime).endswith("123000")
    # 患者出生日期属于 PHI，已删除
    assert not hasattr(ds, "PatientBirthDate")
    # StudyTime 不是日期，保持
    assert str(ds.StudyTime) == "090000"


def test_get_unknown_job_404(client):
    assert client.get("/jobs/deadbeef").status_code == 404
    assert client.get("/jobs/deadbeef/result").status_code == 404
