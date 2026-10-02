"""额外的像素与日期行为测试。"""

import io
import zipfile

import numpy as np

from conftest import (
    make_instance,
    make_policy,
    make_zip,
    poll_job,
    read_output_by_old_uid,
    submit,
)

SA = "1.2.3.500"
SB = "1.2.3.600"
SER = "1.2.3.500.1"
IA = "1.2.3.500.1.1"
IB = "1.2.3.600.1.1"
SERB = "1.2.3.600.1"


def test_per_study_shifts_differ_but_consistent_within_study(client):
    pixel = np.full((16, 16), 99, dtype=np.uint8)

    def set_dates(ds):
        ds.StudyDate = "20200301"
        ds.SeriesDate = "20200305"

    a = make_instance(SA, SER, IA, pixel=pixel, extra_fn=set_dates)
    b = make_instance(SB, SERB, IB, pixel=pixel, extra_fn=set_dates)
    z = make_zip({"a.dcm": a, "b.dcm": b})
    job = poll_job(client, submit(client, z, make_policy())["job_id"])
    assert job["status"] == "completed", job["audit"]["issues"]
    studies = {s["old"]: s["date_shift_days"]
               for s in job["audit"]["structure"]["studies"]}
    # 两个 study 的确定性平移量应当不同（HMAC 派生）
    assert studies[SA] != studies[SB]

    out = client.get(f"/jobs/{job['job_id']}/result").content
    with zipfile.ZipFile(io.BytesIO(out)) as zf:
        da = read_output_by_old_uid(zf, job["audit"], IA)
        db = read_output_by_old_uid(zf, job["audit"], IB)
    # 同一实例内 StudyDate 与 SeriesDate 原来相差 4 天，平移后仍差 4 天
    import datetime as dt

    def parse(v):
        return dt.date(int(v[:4]), int(v[4:6]), int(v[6:8]))

    delta_a = parse(str(da.SeriesDate)) - parse(str(da.StudyDate))
    assert delta_a.days == 4
    delta_b = parse(str(db.SeriesDate)) - parse(str(db.StudyDate))
    assert delta_b.days == 4


def test_rgb_occlusion_overwrites_color_pixels(client):
    rgb = np.zeros((20, 30, 3), dtype=np.uint8)
    rgb[:, :, 0] = 10   # R
    rgb[:, :, 1] = 20   # G
    rgb[:, :, 2] = 30   # B
    a = make_instance(SA, SER, IA, pixel=rgb, photometric="RGB")
    z = make_zip({"a.dcm": a})
    policy = make_policy(occlusions=[
        {"study_uid": SA, "series_uid": SER, "sop_instance_uid": IA,
         "box": {"x": 5, "y": 4, "width": 6, "height": 3, "fill": 7}},
    ])
    job = poll_job(client, submit(client, z, policy)["job_id"])
    assert job["status"] == "completed", job["audit"]["issues"]
    out = client.get(f"/jobs/{job['job_id']}/result").content
    with zipfile.ZipFile(io.BytesIO(out)) as zf:
        ds = read_output_by_old_uid(zf, job["audit"], IA)
    arr = ds.pixel_array
    assert arr.shape == (20, 30, 3)
    region = arr[4:7, 5:11]
    assert (region == 7).all()
    assert tuple(arr[0, 0]) == (10, 20, 30)  # 框外像素不变


def test_multiframe_occlusion_single_frame_only(client):
    # 未压缩 DICOM 多帧像素的原生帧主序字节布局
    frames = np.stack([
        np.full((10, 10), 50, dtype=np.uint8),
        np.full((10, 10), 90, dtype=np.uint8),
    ]).tobytes()
    from pydicom.dataset import Dataset, FileMetaDataset
    from pydicom.uid import ExplicitVRLittleEndian, UID

    ds = Dataset()
    ds.file_meta = FileMetaDataset()
    ds.file_meta.MediaStorageSOPClassUID = UID("1.2.840.10008.5.1.4.1.1.7")
    ds.file_meta.MediaStorageSOPInstanceUID = UID(IA)
    ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds.StudyInstanceUID = SA
    ds.SeriesInstanceUID = SER
    ds.SOPInstanceUID = IA
    ds.SOPClassUID = UID("1.2.840.10008.5.1.4.1.1.7")
    ds.Modality = "OT"
    ds.PatientName = "Doe^John"
    ds.Rows = 10
    ds.Columns = 10
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.BitsAllocated = 8
    ds.BitsStored = 8
    ds.HighBit = 7
    ds.PixelRepresentation = 0
    ds.NumberOfFrames = 2
    ds.StudyDate = "20200101"
    ds.PixelData = frames
    buf = io.BytesIO()
    ds.save_as(buf, enforce_file_format=True)
    z = make_zip({"a.dcm": buf.getvalue()})
    policy = make_policy(occlusions=[
        {"study_uid": SA, "series_uid": SER, "sop_instance_uid": IA,
         "frames": [1],
         "box": {"x": 0, "y": 0, "width": 4, "height": 4, "fill": 0}},
    ])
    job = poll_job(client, submit(client, z, policy)["job_id"])
    assert job["status"] == "completed", job["audit"]["issues"]
    out = client.get(f"/jobs/{job['job_id']}/result").content
    with zipfile.ZipFile(io.BytesIO(out)) as zf:
        ds = read_output_by_old_uid(zf, job["audit"], IA)
    arr = ds.pixel_array
    assert arr.shape == (2, 10, 10)
    assert arr[1, 0:4, 0:4].max() == 0   # 第 2 帧被遮挡
    assert arr[1, 5, 5] == 90
    assert arr[0, 0:4, 0:4].max() == 50  # 第 1 帧保持
