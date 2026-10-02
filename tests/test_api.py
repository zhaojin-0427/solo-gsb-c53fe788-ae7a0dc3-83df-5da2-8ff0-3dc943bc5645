"""End-to-end and unit tests for the anonymization API."""
from __future__ import annotations

import hashlib
import io
import json
import zipfile
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from pydicom import dcmread
from pydicom.dataset import Dataset, FileDataset, FileMetaDataset
from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian, JPEGBaseline

from app.config import Settings
from app.dicom_ops import UIDMapper, compute_offset
from app.engine import anonymize_zip
from app.main import create_app
from app.policy import canonical_json

SECRET = "test-secret"
BASE_POLICY = {
    "version": "1.0",
    "salt": "unit-test",
    "date_shift": {"enabled": True, "max_days": 365},
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def make_dcm(
    study_uid,
    series_uid,
    sop_uid,
    *,
    patient_id="PAT-001",
    patient_name="Doe^John",
    study_date="20200101",
    study_time="120000",
    modality="CT",
    rows=64,
    cols=64,
    bits=8,
    pixel_value=255,
    instance_number=1,
    transfer_syntax=ExplicitVRLittleEndian,
    with_private=False,
    referenced_uid=None,
    omit=(),
):
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = CTImageStorage
    meta.MediaStorageSOPInstanceUID = sop_uid
    meta.TransferSyntaxUID = transfer_syntax
    meta.ImplementationClassUID = "2.25.999"
    ds = FileDataset("mem", {}, file_meta=meta, preamble=b"\x00" * 128)
    ds.SOPClassUID = CTImageStorage
    ds.SOPInstanceUID = sop_uid
    ds.StudyInstanceUID = study_uid
    ds.SeriesInstanceUID = series_uid
    ds.PatientName = patient_name
    ds.PatientID = patient_id
    ds.PatientBirthDate = "19800101"
    ds.PatientSex = "M"
    ds.Modality = modality
    ds.StudyDate = study_date
    ds.StudyTime = study_time
    ds.SeriesDate = study_date
    ds.ContentDate = study_date
    ds.ReferringPhysicianName = "Smith^Alice"
    ds.InstitutionName = "General Hospital"
    ds.AccessionNumber = "ACC123"
    ds.StudyID = "STUDY1"
    ds.InstanceNumber = instance_number
    ds.Rows = rows
    ds.Columns = cols
    ds.BitsAllocated = bits
    ds.BitsStored = bits
    ds.HighBit = bits - 1
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.PixelRepresentation = 0
    if bits == 8:
        ds.PixelData = bytes([pixel_value & 0xFF]) * (rows * cols)
    else:
        ds.PixelData = (pixel_value & 0xFFFF).to_bytes(2, "little") * (rows * cols)
    if transfer_syntax not in (ExplicitVRLittleEndian,):
        # compressed/encapsulated syntaxes require encapsulated pixel data
        from pydicom.encaps import encapsulate

        ds.PixelData = encapsulate([ds.PixelData])
    if with_private:
        ds.add_new(0x00090010, "LO", "ACME")
        ds.add_new(0x00091001, "LO", "SECRET-INFO")
    if referenced_uid:
        ref = Dataset()
        ref.ReferencedSOPClassUID = CTImageStorage
        ref.ReferencedSOPInstanceUID = referenced_uid
        ds.ReferencedSOPSequence = [ref]
    for keyword in omit:
        if keyword in ds:
            del ds[keyword]
    ds.is_little_endian = True
    ds.is_implicit_VR = False
    buf = io.BytesIO()
    ds.save_as(buf, write_like_original=False)
    return buf.getvalue()


def make_zip(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


@pytest.fixture()
def client(tmp_path):
    app = create_app(Settings(data_dir=str(tmp_path), secret=SECRET))
    with TestClient(app) as test_client:
        yield test_client


def submit(client, zip_bytes, policy, redactions=None):
    data = {"policy": json.dumps(policy)}
    if redactions is not None:
        data["redactions"] = json.dumps(redactions)
    response = client.post(
        "/jobs",
        files={"file": ("input.zip", zip_bytes, "application/zip")},
        data=data,
    )
    assert response.status_code in (200, 202), response.text
    return response.json()["job_id"]


def get_job(client, job_id):
    response = client.get(f"/jobs/{job_id}")
    assert response.status_code == 200, response.text
    return response.json()


def download(client, job_id):
    response = client.get(f"/jobs/{job_id}/download")
    assert response.status_code == 200, response.text
    return response.content


def read_output_instances(zip_bytes):
    zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    out = {}
    for name in zf.namelist():
        if name.endswith(".dcm"):
            out[name] = dcmread(io.BytesIO(zf.read(name)))
    return out


def three_instance_zip():
    study, series_a, series_b = "1.2.3.1", "1.2.3.2", "1.2.3.3"
    files = {
        "b/ct2.dcm": make_dcm(study, series_a, "1.2.3.11", study_date="20200105", instance_number=2),
        "a/ct1.dcm": make_dcm(study, series_a, "1.2.3.10", study_date="20200101", instance_number=1),
        "c/mr1.dcm": make_dcm(study, series_b, "1.2.3.12", study_date="20200103", instance_number=1, modality="MR"),
    }
    return make_zip(files)


def policy_key(policy):
    return hashlib.sha256(f"{SECRET}|{canonical_json(policy)}".encode("utf-8")).digest()


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------
def test_happy_path_anonymizes_and_audits(client):
    job_id = submit(client, three_instance_zip(), BASE_POLICY)
    status = get_job(client, job_id)
    assert status["status"] == "done"
    assert status["policy_version"] == "1.0"

    audit = status["audit"]
    assert audit["summary"]["studies"] == 1
    assert audit["summary"]["series"] == 2
    assert audit["summary"]["instances"] == 3
    assert audit["summary"]["tags_emptied"] > 0
    assert audit["summary"]["tags_hashed"] == 3  # PatientID per instance
    assert audit["date_shift"]["enabled"] is True

    result_zip = download(client, job_id)
    assert hashlib.sha256(result_zip).hexdigest() == status["result"]["sha256"]
    assert hashlib.sha256(result_zip).hexdigest() == audit["output"]["zip_sha256"]

    zf = zipfile.ZipFile(io.BytesIO(result_zip))
    names = zf.namelist()
    assert names[0] == "manifest.json"
    dcms = [n for n in names if n.endswith(".dcm")]
    assert dcms == [
        "STUDY_0001/SERIES_0001/INSTANCE_00001.dcm",
        "STUDY_0001/SERIES_0001/INSTANCE_00002.dcm",
        "STUDY_0001/SERIES_0002/INSTANCE_00001.dcm",
    ]
    manifest = json.loads(zf.read("manifest.json"))
    assert len(manifest["files"]) == 3
    for entry in manifest["files"]:
        payload = zf.read(entry["path"])
        assert hashlib.sha256(payload).hexdigest() == entry["sha256"]

    mapper = UIDMapper(policy_key(BASE_POLICY))
    offset = compute_offset(policy_key(BASE_POLICY), "PAT-001", 365)
    expected_dates = {
        (datetime.strptime(d, "%Y%m%d") + offset).strftime("%Y%m%d")
        for d in ("20200101", "20200105", "20200103")
    }
    seen_dates = set()
    for ds in read_output_instances(result_zip).values():
        # PHI cleared
        assert str(getattr(ds, "PatientName", "")) == ""
        assert str(getattr(ds, "PatientBirthDate", "")) == ""
        assert str(getattr(ds, "ReferringPhysicianName", "")) == ""
        assert str(getattr(ds, "InstitutionName", "")) == ""
        assert "AccessionNumber" not in ds or str(ds.AccessionNumber) == ""
        # PatientID hashed, not plaintext
        assert str(ds.PatientID) != "PAT-001"
        assert len(str(ds.PatientID)) == 16
        # deterministic UID mapping, consistent with file meta
        assert str(ds.StudyInstanceUID) == mapper.map("1.2.3.1")
        assert str(ds.SOPInstanceUID).startswith("2.25.")
        assert str(ds.file_meta.MediaStorageSOPInstanceUID) == str(ds.SOPInstanceUID)
        assert str(ds.file_meta.TransferSyntaxUID) == str(ExplicitVRLittleEndian)
        # non-PHI attributes survive
        assert ds.Modality in ("CT", "MR")
        assert str(ds.PatientSex) == "M"
        seen_dates.add(str(ds.StudyDate))
    # dates shifted by the expected deterministic offset, deltas preserved
    assert seen_dates == expected_dates


def test_date_delta_preserved_exactly(client):
    study, series = "1.5.1", "1.5.2"
    files = {
        "d1.dcm": make_dcm(study, series, "1.5.10", study_date="20200101", instance_number=1),
        "d2.dcm": make_dcm(study, series, "1.5.11", study_date="20200111", instance_number=2),
    }
    job_id = submit(client, make_zip(files), BASE_POLICY)
    assert get_job(client, job_id)["status"] == "done"
    instances = read_output_instances(download(client, job_id))
    dates = sorted(
        datetime.strptime(str(ds.StudyDate), "%Y%m%d") for ds in instances.values()
    )
    assert (dates[1] - dates[0]).days == 10


def test_pixel_redaction_burns_box(client):
    files = {"img.dcm": make_dcm("1.9.1", "1.9.2", "1.9.3", rows=64, cols=64, pixel_value=200)}
    redactions = [{"scope": "instance", "uid": "1.9.3", "boxes": [[10, 10, 20, 20]], "fill": 0}]
    job_id = submit(client, make_zip(files), BASE_POLICY, redactions)
    status = get_job(client, job_id)
    assert status["status"] == "done"
    assert status["audit"]["summary"]["redaction_boxes_applied"] == 1
    (ds,) = read_output_instances(download(client, job_id)).values()
    pixels = ds.PixelData
    for row in range(64):
        for col in range(64):
            value = pixels[row * 64 + col]
            if 10 <= row < 30 and 10 <= col < 30:
                assert value == 0, f"pixel ({col},{row}) not redacted"
            else:
                assert value == 200, f"pixel ({col},{row}) unexpectedly modified"


def test_pixel_redaction_16bit(client):
    files = {"img16.dcm": make_dcm("1.10.1", "1.10.2", "1.10.3", rows=32, cols=32, bits=16, pixel_value=4000)}
    redactions = [{"scope": "series", "uid": "1.10.2", "boxes": [[0, 0, 8, 8]], "fill": 100}]
    job_id = submit(client, make_zip(files), BASE_POLICY, redactions)
    assert get_job(client, job_id)["status"] == "done"
    (ds,) = read_output_instances(download(client, job_id)).values()
    pixels = ds.PixelData
    for row in range(32):
        for col in range(32):
            value = int.from_bytes(pixels[(row * 32 + col) * 2 : (row * 32 + col) * 2 + 2], "little")
            assert value == (100 if row < 8 and col < 8 else 4000)


def test_private_tags_removed(client):
    files = {"p.dcm": make_dcm("1.11.1", "1.11.2", "1.11.3", with_private=True)}
    job_id = submit(client, make_zip(files), BASE_POLICY)
    status = get_job(client, job_id)
    assert status["status"] == "done"
    assert status["audit"]["summary"]["private_tags_removed"] >= 2
    (ds,) = read_output_instances(download(client, job_id)).values()
    assert all(elem.tag.group % 2 == 0 for elem in ds)


def test_referenced_uid_remapped_consistently(client):
    study, series = "1.12.1", "1.12.2"
    files = {
        "a.dcm": make_dcm(study, series, "1.12.10", instance_number=1),
        "b.dcm": make_dcm(study, series, "1.12.11", instance_number=2, referenced_uid="1.12.10"),
    }
    job_id = submit(client, make_zip(files), BASE_POLICY)
    assert get_job(client, job_id)["status"] == "done"
    mapper = UIDMapper(policy_key(BASE_POLICY))
    instances = read_output_instances(download(client, job_id))
    refs = [
        str(ds.ReferencedSOPSequence[0].ReferencedSOPInstanceUID)
        for ds in instances.values()
        if "ReferencedSOPSequence" in ds
    ]
    assert refs == [mapper.map("1.12.10")]


# ---------------------------------------------------------------------------
# Determinism / idempotency
# ---------------------------------------------------------------------------
def test_same_input_same_job_and_bytes(client):
    payload = three_instance_zip()
    job_a = submit(client, payload, BASE_POLICY)
    job_b = submit(client, payload, BASE_POLICY)
    assert job_a == job_b
    status = get_job(client, job_a)
    assert status["status"] == "done"
    assert hashlib.sha256(download(client, job_a)).hexdigest() == status["result"]["sha256"]


def test_engine_is_byte_deterministic():
    payload = three_instance_zip()
    errors1, result1, audit1, _ = anonymize_zip(payload, BASE_POLICY, None, SECRET)
    errors2, result2, audit2, _ = anonymize_zip(payload, BASE_POLICY, None, SECRET)
    assert errors1 == errors2 == []
    assert result1 == result2
    assert audit1 == audit2


def test_different_policy_changes_output():
    payload = three_instance_zip()
    other_policy = dict(BASE_POLICY, salt="other-project")
    _, result1, _, _ = anonymize_zip(payload, BASE_POLICY, None, SECRET)
    _, result2, _, _ = anonymize_zip(payload, other_policy, None, SECRET)
    assert result1 != result2


# ---------------------------------------------------------------------------
# Itemized failures (no partial output)
# ---------------------------------------------------------------------------
def test_compressed_pixels_rejected(client):
    files = {"c.dcm": make_dcm("1.20.1", "1.20.2", "1.20.3", transfer_syntax=JPEGBaseline)}
    job_id = submit(client, make_zip(files), BASE_POLICY)
    status = get_job(client, job_id)
    assert status["status"] == "failed"
    assert any(e["code"] == "COMPRESSED_PIXELS" and e["file"] == "c.dcm" for e in status["errors"])
    assert client.get(f"/jobs/{job_id}/download").status_code == 409


def test_missing_reference_rejected(client):
    files = {"r.dcm": make_dcm("1.21.1", "1.21.2", "1.21.3", referenced_uid="9.9.9")}
    job_id = submit(client, make_zip(files), BASE_POLICY)
    status = get_job(client, job_id)
    assert status["status"] == "failed"
    assert any(e["code"] == "MISSING_REFERENCE" and "9.9.9" in e["message"] for e in status["errors"])


def test_missing_required_uid_rejected(client):
    files = {"m.dcm": make_dcm("1.22.1", "1.22.2", "1.22.3", omit=("SeriesInstanceUID",))}
    job_id = submit(client, make_zip(files), BASE_POLICY)
    status = get_job(client, job_id)
    assert status["status"] == "failed"
    assert any(e["code"] == "MISSING_UID" for e in status["errors"])


def test_duplicate_instance_rejected(client):
    files = {
        "x.dcm": make_dcm("1.23.1", "1.23.2", "1.23.3"),
        "y.dcm": make_dcm("1.23.1", "1.23.2", "1.23.3"),
    }
    job_id = submit(client, make_zip(files), BASE_POLICY)
    status = get_job(client, job_id)
    assert status["status"] == "failed"
    assert any(e["code"] == "DUPLICATE_INSTANCE" for e in status["errors"])


def test_illegal_vr_value_rejected(client):
    files = {"bad.dcm": make_dcm("1.24.1", "1.24.2", "1.24.3", study_date="20201340")}
    job_id = submit(client, make_zip(files), BASE_POLICY)
    status = get_job(client, job_id)
    assert status["status"] == "failed"
    assert any(e["code"] == "INVALID_VALUE" and e["tag"] for e in status["errors"])


def test_policy_conflict_rejected(client):
    policy = dict(BASE_POLICY, tags={"keep": ["PatientName"], "remove": ["PatientName"]})
    job_id = submit(client, three_instance_zip(), policy)
    status = get_job(client, job_id)
    assert status["status"] == "failed"
    assert any(e["code"] == "POLICY_CONFLICT" for e in status["errors"])


def test_policy_cannot_touch_managed_tags(client):
    policy = dict(BASE_POLICY, tags={"remove": ["StudyInstanceUID"]})
    job_id = submit(client, three_instance_zip(), policy)
    status = get_job(client, job_id)
    assert status["status"] == "failed"
    assert any(e["code"] == "POLICY_CONFLICT" for e in status["errors"])


def test_bad_replace_value_rejected(client):
    policy = dict(BASE_POLICY, tags={"replace": {"PatientBirthDate": "not-a-date"}})
    job_id = submit(client, three_instance_zip(), policy)
    status = get_job(client, job_id)
    assert status["status"] == "failed"
    assert any(e["code"] == "POLICY_CONFLICT" for e in status["errors"])


def test_unsupported_policy_version_rejected(client):
    job_id = submit(client, three_instance_zip(), {"version": "99.0"})
    status = get_job(client, job_id)
    assert status["status"] == "failed"
    assert any(e["code"] == "POLICY_INVALID" for e in status["errors"])


def test_redaction_out_of_bounds_rejected(client):
    files = {"img.dcm": make_dcm("1.25.1", "1.25.2", "1.25.3", rows=64, cols=64)}
    redactions = [{"scope": "instance", "uid": "1.25.3", "boxes": [[60, 60, 10, 10]]}]
    job_id = submit(client, make_zip(files), BASE_POLICY, redactions)
    status = get_job(client, job_id)
    assert status["status"] == "failed"
    assert any(e["code"] == "REDACTION_OUT_OF_BOUNDS" for e in status["errors"])


def test_redaction_target_not_found(client):
    redactions = [{"scope": "instance", "uid": "8.8.8", "boxes": [[0, 0, 5, 5]]}]
    job_id = submit(client, three_instance_zip(), BASE_POLICY, redactions)
    status = get_job(client, job_id)
    assert status["status"] == "failed"
    assert any(e["code"] == "REDACTION_TARGET_NOT_FOUND" for e in status["errors"])


def test_invalid_zip_rejected(client):
    job_id = submit(client, b"this is not a zip file", BASE_POLICY)
    status = get_job(client, job_id)
    assert status["status"] == "failed"
    assert any(e["code"] == "ZIP_INVALID" for e in status["errors"])


def test_multiple_errors_reported_together(client):
    files = {
        "bad1.dcm": make_dcm("1.26.1", "1.26.2", "1.26.3", study_date="20201340"),
        "bad2.dcm": make_dcm("1.26.1", "1.26.2", "1.26.4", transfer_syntax=JPEGBaseline),
    }
    job_id = submit(client, make_zip(files), BASE_POLICY)
    status = get_job(client, job_id)
    assert status["status"] == "failed"
    codes = {e["code"] for e in status["errors"]}
    assert {"INVALID_VALUE", "COMPRESSED_PIXELS"} <= codes


# ---------------------------------------------------------------------------
# Misc API behaviour
# ---------------------------------------------------------------------------
def test_unknown_job_returns_404(client):
    assert client.get("/jobs/does-not-exist").status_code == 404


def test_invalid_policy_json_returns_400(client):
    response = client.post(
        "/jobs",
        files={"file": ("input.zip", three_instance_zip(), "application/zip")},
        data={"policy": "{not json"},
    )
    assert response.status_code == 400


def test_healthz_and_policies(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    policies = client.get("/policies").json()
    assert policies["supported_versions"] == ["1.0"]
    assert "empty" in policies["default_profile"]
