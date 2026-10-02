"""端到端测试夹具：构造合成多序列 DICOM ZIP。"""

import io
import os
import zipfile

import numpy as np
import pytest
from pydicom import dcmread
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.encaps import encapsulate
from pydicom.uid import (
    ExplicitVRLittleEndian,
    JPEGBaseline8Bit,
    UID,
)

# 必须在导入应用之前指向临时数据目录
_TMP_DATA = os.environ.setdefault("DICOM_ANON_DATA_DIR", "")
if not _TMP_DATA:
    import tempfile

    os.environ["DICOM_ANON_DATA_DIR"] = tempfile.mkdtemp(prefix="dicom-anon-test-")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:
        yield c


def _base_ds(study_uid, series_uid, sop_uid, *, photometric="MONOCHROME2",
             pixel=None, bits=8, extra_fn=None, transfer_syntax=ExplicitVRLittleEndian,
             number_of_frames=None):
    ds = Dataset()
    ds.file_meta = FileMetaDataset()
    ds.file_meta.MediaStorageSOPClassUID = UID("1.2.840.10008.5.1.4.1.1.7")
    ds.file_meta.MediaStorageSOPInstanceUID = UID(sop_uid)
    ds.file_meta.TransferSyntaxUID = transfer_syntax
    ds.file_meta.SourceApplicationEntityTitle = "SECRET_AE"

    ds.StudyInstanceUID = study_uid
    ds.SeriesInstanceUID = series_uid
    ds.SOPInstanceUID = sop_uid
    ds.SOPClassUID = UID("1.2.840.10008.5.1.4.1.1.7")
    ds.Modality = "OT"
    ds.PatientName = "Doe^John"
    ds.PatientID = "P-0001"
    ds.PatientBirthDate = "19800505"
    ds.PatientSex = "M"
    ds.PatientAddress = "1 Secret Street"
    ds.StudyDate = "20200115"
    ds.SeriesDate = "20200115"
    ds.AcquisitionDateTime = "20200115123000"
    ds.StudyTime = "090000"
    ds.InstitutionName = "Secret Hospital"
    ds.StationName = "SECRET-STATION"
    ds.AccessionNumber = "ACC-9999"
    ds.StudyDescription = "Confidential study"
    ds.ImageComments = "burned text maybe"
    ds.Manufacturer = "SecretCorp"
    ds.ContentDate = "20200116"

    if pixel is None:
        pixel = np.full((32, 64), 200, dtype=np.uint8)
    ds.Rows = pixel.shape[0]
    ds.Columns = pixel.shape[1]
    ds.SamplesPerPixel = 3 if photometric in ("RGB", "YBR_FULL") else 1
    ds.PhotometricInterpretation = photometric
    if ds.SamplesPerPixel == 3:
        ds.PlanarConfiguration = 0  # 交错像素
    ds.BitsAllocated = bits
    ds.BitsStored = bits
    ds.HighBit = bits - 1
    ds.PixelRepresentation = 0
    if number_of_frames is not None:
        ds.NumberOfFrames = number_of_frames
    if str(transfer_syntax) == str(JPEGBaseline8Bit):
        # 伪造的 JPEG 封装片段；测试只到“检测到压缩传输语法”为止
        ds.PixelData = encapsulate([b"\xff\xd8\xff\xe0fakejpeg\xff\xd9"])
        ds["PixelData"].is_undefined_length = True
    else:
        ds.PixelData = pixel.tobytes()

    if extra_fn:
        extra_fn(ds)
    return ds


def save_to_bytes(ds) -> bytes:
    buf = io.BytesIO()
    ds.save_as(buf, write_like_original=False)
    return buf.getvalue()


def make_instance(study_uid, series_uid, sop_uid, **kw) -> bytes:
    ds = _base_ds(study_uid, series_uid, sop_uid, **kw)
    return save_to_bytes(ds)


def make_zip(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


def add_reference(ds, referenced_sop: str):
    """按标准结构在 CurrentRequestedProcedureEvidenceSequence 里加引用。"""
    evidence_item = Dataset()
    evidence_item.StudyInstanceUID = ds.StudyInstanceUID
    ref_series = Dataset()
    ref_series.SeriesInstanceUID = ds.SeriesInstanceUID
    ref = Dataset()
    ref.ReferencedSOPClassUID = UID("1.2.840.10008.5.1.4.1.1.7")
    ref.ReferencedSOPInstanceUID = referenced_sop
    ref_series.ReferencedSOPSequence = [ref]
    evidence_item.ReferencedSeriesSequence = [ref_series]
    ds.CurrentRequestedProcedureEvidenceSequence = [evidence_item]


def add_private(ds: Dataset) -> None:
    ds.add_new(0x00090010, "LO", "PRIVATE_CREATOR")
    ds.add_new(0x00091010, "LO", "SECRET_PRIVATE_VALUE")


def make_policy(**overrides) -> bytes:
    import json

    policy = {
        "policy_version": "1.0",
        "policy_id": "test-policy-1",
        "keep_tags": ["PatientSex"],
        "remove_tags": [],
        "occlusions": [],
        "dates": {"shift": True, "jitter_max_days": 180, "min_year": 1901},
    }
    policy.update(overrides)
    return json.dumps(policy).encode("utf-8")


def poll_job(client, job_id: str, timeout: float = 30.0) -> dict:
    import time

    deadline = time.time() + timeout
    while True:
        resp = client.get(f"/jobs/{job_id}")
        assert resp.status_code == 200
        body = resp.json()
        if body["status"] not in ("queued", "processing"):
            return body
        assert time.time() < deadline, f"job {job_id} 超时未完成"
        time.sleep(0.1)


def submit(client, zip_bytes: bytes, policy_bytes: bytes) -> dict:
    resp = client.post(
        "/jobs",
        files={
            "file": ("study.zip", zip_bytes, "application/zip"),
            "policy": ("policy.json", policy_bytes, "application/json"),
        },
    )
    assert resp.status_code == 202, resp.text
    return resp.json()


def read_output_by_old_uid(zf: zipfile.ZipFile, audit: dict, old_uid: str):
    """按原始 SOP Instance UID 从输出 ZIP 中读回匿名化实例。"""
    new_uid = audit["uid_maps"]["instance"][old_uid]
    name = next(n for n in zf.namelist() if n.endswith(f"/{new_uid}.dcm"))
    return dcmread(io.BytesIO(zf.read(name)))
