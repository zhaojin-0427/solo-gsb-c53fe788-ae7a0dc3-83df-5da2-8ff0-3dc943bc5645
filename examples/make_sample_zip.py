#!/usr/bin/env python3
"""生成一个包含多 Study/Series/Instance 的示例 DICOM ZIP。

用法：
    pip install pydicom pillow numpy
    python examples/make_sample_zip.py sample.zip

生成的内容含标准 PHI、私有标签、日期与可辨认像素图案，
配合 examples/policy.example.json 可直接演示匿名化流程。
"""

import io
import sys
import zipfile

import numpy as np
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, UID


def build_instance(study_uid, series_uid, sop_uid, pattern, rows=64, cols=64):
    ds = Dataset()
    ds.file_meta = FileMetaDataset()
    ds.file_meta.MediaStorageSOPClassUID = UID("1.2.840.10008.5.1.4.1.1.7")
    ds.file_meta.MediaStorageSOPInstanceUID = UID(sop_uid)
    ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds.file_meta.SourceApplicationEntityTitle = "CLINIC_AE"

    ds.StudyInstanceUID = UID(study_uid)
    ds.SeriesInstanceUID = UID(series_uid)
    ds.SOPInstanceUID = UID(sop_uid)
    ds.SOPClassUID = UID("1.2.840.10008.5.1.4.1.1.7")
    ds.Modality = "OT"
    ds.PatientName = "Zhang^San"
    ds.PatientID = "MRN-2026-0001"
    ds.PatientBirthDate = "19850312"
    ds.PatientSex = "F"
    ds.PatientAddress = "88 Renmin Road, Shenzhen"
    ds.PatientTelephoneNumbers = "0755-00000000"
    ds.StudyDate = "20260115"
    ds.SeriesDate = "20260115"
    ds.AcquisitionDateTime = "20260115103000"
    ds.StudyTime = "103000"
    ds.ContentDate = "20260116"
    ds.InstitutionName = "Example Research Hospital"
    ds.InstitutionAddress = "1 Example Ave"
    ds.StationName = "STATION-07"
    ds.AccessionNumber = "ACC-2026-0188"
    ds.StudyDescription = "Confidential research study"
    ds.ImageComments = "free text may contain PHI"
    ds.Manufacturer = "Acme Imaging"
    ds.DeviceSerialNumber = "SN-12345"
    ds.ReferringPhysicianName = "Dr^Li"
    ds.OperatorsName = "Op^Wang"

    ds.Rows = rows
    ds.Columns = cols
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.BitsAllocated = 8
    ds.BitsStored = 8
    ds.HighBit = 7
    ds.PixelRepresentation = 0

    if pattern == "gradient":
        pixel = (np.arange(rows * cols, dtype=np.uint16) % 256).reshape(
            rows, cols
        ).astype(np.uint8)
    elif pattern == "solid":
        pixel = np.full((rows, cols), 170, dtype=np.uint8)
    else:
        pixel = np.zeros((rows, cols), dtype=np.uint8)
        pixel[10:20, 10:40] = 255  # 白色“烧录文字条”带，可演示遮挡
    ds.PixelData = pixel.tobytes()

    # 私有标签
    ds.add_new(0x00090010, "LO", "ACME_CORP")
    ds.add_new(0x00091001, "LO", "internal-patient-key-999")

    buf = io.BytesIO()
    ds.save_as(buf, enforce_file_format=True)
    return buf.getvalue()


def main(path: str) -> None:
    study = "1.2.3.400"
    s1 = study + ".1"
    s2 = study + ".2"
    members = {
        "study/series1/0001.dcm": build_instance(
            study, s1, s1 + ".1", "gradient"),
        "study/series1/0002.dcm": build_instance(
            study, s1, s1 + ".2", "solid"),
        "study/series2/0001.dcm": build_instance(
            study, s2, s2 + ".1", "banner"),
    }
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    print(f"wrote {path} with {len(members)} instances")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "sample.zip")
