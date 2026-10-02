#!/usr/bin/env python3
"""Generate a deterministic sample DICOM ZIP for trying the API.

Creates ``sample_input.zip`` (default, in the current directory) containing
one study with two series x two instances of synthetic 128x128 16-bit CT
images, complete with fake PHI, a private tag and dates spread over several
days. The UIDs are fixed so examples/redactions.json matches series 1.

Usage: python scripts/make_sample.py [output.zip]
"""
from __future__ import annotations

import io
import sys
import zipfile

from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian

STUDY_UID = "1.2.826.0.1.3680043.9.7433.1.1"
SERIES = [
    ("1.2.826.0.1.3680043.9.7433.1.1.1", "CT HEAD", ["1.2.826.0.1.3680043.9.7433.1.1.1.1",
                                                      "1.2.826.0.1.3680043.9.7433.1.1.1.2"]),
    ("1.2.826.0.1.3680043.9.7433.1.1.2", "CT CHEST", ["1.2.826.0.1.3680043.9.7433.1.1.2.1",
                                                       "1.2.826.0.1.3680043.9.7433.1.1.2.2"]),
]
DATES = [("20200103", "101500"), ("20200103", "101600"), ("20200107", "093000"), ("20200107", "093100")]


def make_instance(series_uid, sop_uid, description, date, time, instance_number):
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = CTImageStorage
    meta.MediaStorageSOPInstanceUID = sop_uid
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    meta.ImplementationClassUID = "2.25.1234567890"
    ds = FileDataset("sample", {}, file_meta=meta, preamble=b"\x00" * 128)
    ds.SOPClassUID = CTImageStorage
    ds.SOPInstanceUID = sop_uid
    ds.StudyInstanceUID = STUDY_UID
    ds.SeriesInstanceUID = series_uid
    ds.PatientName = "Doe^John"
    ds.PatientID = "PAT-12345"
    ds.PatientBirthDate = "19750312"
    ds.PatientSex = "M"
    ds.PatientAge = "045Y"
    ds.Modality = "CT"
    ds.StudyDate = date
    ds.StudyTime = time
    ds.SeriesDate = date
    ds.ContentDate = date
    ds.StudyDescription = description
    ds.SeriesDescription = description
    ds.ReferringPhysicianName = "Smith^Alice"
    ds.InstitutionName = "General Hospital"
    ds.AccessionNumber = "ACC-998877"
    ds.StudyID = "S123"
    ds.InstanceNumber = instance_number
    ds.Rows = 128
    ds.Columns = 128
    ds.BitsAllocated = 16
    ds.BitsStored = 16
    ds.HighBit = 15
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.PixelRepresentation = 0
    # synthetic gradient with a bright "annotation" band on the first rows
    pixels = bytearray()
    for row in range(128):
        for col in range(128):
            value = (row * 16 + col) % 2048
            if row < 30:
                value = 4000  # burned-in text area
            pixels += value.to_bytes(2, "little")
    ds.PixelData = bytes(pixels)
    ds.add_new(0x00090010, "LO", "ACME_SCANNER")
    ds.add_new(0x00091001, "LO", "internal-scan-note")
    ds.is_little_endian = True
    ds.is_implicit_VR = False
    buf = io.BytesIO()
    ds.save_as(buf, write_like_original=False)
    return buf.getvalue()


def main() -> None:
    out_path = sys.argv[1] if len(sys.argv) > 1 else "sample_input.zip"
    idx = 0
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for series_idx, (series_uid, description, sop_uids) in enumerate(SERIES, 1):
            for inst_idx, sop_uid in enumerate(sop_uids, 1):
                date, time = DATES[idx]
                idx += 1
                payload = make_instance(series_uid, sop_uid, description, date, time, inst_idx)
                zf.writestr(f"series{series_idx}/instance{inst_idx}.dcm", payload)
    print(f"wrote {out_path}")
    print(f"  study UID:  {STUDY_UID}")
    for series_uid, description, sop_uids in SERIES:
        print(f"  series UID: {series_uid} ({description}) -> {len(sop_uids)} instances")


if __name__ == "__main__":
    main()
