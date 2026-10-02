"""输入校验与边界测试。"""

from conftest import make_instance, make_policy, make_zip, poll_job, submit
from test_api import I1, S1, STUDY


def test_bad_zip_rejected(client):
    job = poll_job(client, submit(client, b"not a zip at all",
                                  make_policy())["job_id"])
    assert job["status"] == "failed"
    assert any(i["code"] == "zip_invalid" for i in job["audit"]["issues"])


def test_malformed_policy_json_reported(client):
    z = make_zip({"a.dcm": make_instance(STUDY, S1, I1)})
    job = poll_job(client, submit(client, z, b"{not json")["job_id"])
    assert job["status"] == "failed"
    assert any(i["code"] == "policy_invalid"
               for i in job["audit"]["issues"])


def test_policy_unknown_field_rejected(client):
    z = make_zip({"a.dcm": make_instance(STUDY, S1, I1)})
    job = poll_job(client, submit(
        client, z,
        b'{"policy_version":"1.0","policy_id":"p","surprise":1}',
    )["job_id"])
    assert job["status"] == "failed"
    assert any("surprise" in i["message"] for i in job["audit"]["issues"])


def test_empty_uploads_return_400(client):
    r = client.post(
        "/jobs",
        files={
            "file": ("s.zip", b"", "application/zip"),
            "policy": ("p.json", make_policy(), "application/json"),
        },
    )
    assert r.status_code == 400


def test_result_409_while_processing_or_failed(client):
    z = make_zip({"a.dcm": make_instance(STUDY, S1, I1)})
    job = poll_job(client, submit(client, z, b"{bad")["job_id"])
    r = client.get(f"/jobs/{job['job_id']}/result")
    assert r.status_code == 409


def test_multiple_issues_reported_together(client):
    """同一提交里的多个问题必须逐项报告，而不是遇错即停。"""
    # 实例 A 合法；实例 B 压缩；策略目标还指向不存在的 study
    import numpy as np
    from pydicom.uid import JPEGBaseline8Bit

    a = make_instance(STUDY, S1, I1,
                      pixel=np.full((8, 8), 1, dtype=np.uint8))
    b = make_instance(STUDY, S1, "1.2.3.400.1.9",
                      pixel=np.full((8, 8), 1, dtype=np.uint8),
                      transfer_syntax=JPEGBaseline8Bit)
    z = make_zip({"a.dcm": a, "b.dcm": b})
    policy = make_policy(occlusions=[
        {"study_uid": "9.9.9.9",
         "box": {"x": 0, "y": 0, "width": 1, "height": 1}},
    ])
    job = poll_job(client, submit(client, z, policy)["job_id"])
    assert job["status"] == "failed"
    codes = {i["code"] for i in job["audit"]["issues"]}
    assert "compressed_pixel" in codes
    assert "policy_target_missing" in codes
