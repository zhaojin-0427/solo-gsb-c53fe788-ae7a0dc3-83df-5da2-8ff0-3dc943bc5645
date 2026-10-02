"""FastAPI 入口。

接口：
* POST /jobs              multipart 上传 file=<DICOM.zip> policy=<策略 JSON>，
                          返回 202 + job id，后台执行匿名化；
* GET  /jobs/{id}         查询状态、审计摘要、哈希、逐项问题；
* GET  /jobs/{id}/result  完成后下载匿名化 ZIP（失败/处理中返回 409）；
* GET  /health            存活检查。
"""

from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse, Response

from . import __version__
from .config import TMP_DIR, ZIP_DIR, ensure_dirs
from .db import Database
from .issues import IssueList
from .pipeline import run_job
from .schema import SUPPORTED_POLICY_VERSION


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_dirs()
    yield
    _executor.shutdown(wait=False, cancel_futures=True)
    db.close()


app = FastAPI(
    title="DICOM 研究数据匿名化 API",
    version=__version__,
    description="提交多序列 DICOM ZIP + 版本化策略，产出确定性匿名化 ZIP。",
    lifespan=lifespan,
)

db = Database()
_executor = ThreadPoolExecutor(
    max_workers=int(__import__("os").environ.get("DICOM_ANON_WORKERS", "2")),
    thread_name_prefix="anon-job",
)


def _best_effort_policy_id(policy_bytes: bytes) -> str | None:
    import json

    try:
        obj = json.loads(policy_bytes.decode("utf-8"))
        if isinstance(obj, dict):
            pid = obj.get("policy_id")
            return pid if isinstance(pid, str) else None
    except Exception:
        return None
    return None


def _process(job_id: str, zip_bytes: bytes, policy_bytes: bytes) -> None:
    db.mark_processing(job_id, _utc_now())
    ensure_dirs()
    outcome = run_job(job_id, zip_bytes, policy_bytes, ZIP_DIR)
    issues = IssueList(issues=list(outcome.issues))
    db.finish_job(
        job_id,
        status=outcome.status,
        ts=_utc_now(),
        instances_in=outcome.instances_in,
        instances_out=outcome.instances_out,
        studies=outcome.studies,
        series=outcome.series,
        error_count=issues.error_count,
        warning_count=issues.warning_count,
        output_sha256=outcome.output_sha256,
        output_size=outcome.output_size,
        audit=outcome.audit or {"issues": issues.as_dicts()},
    )


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "version": __version__,
        "supported_policy_version": SUPPORTED_POLICY_VERSION,
    }


@app.post("/jobs", status_code=202)
async def create_job(
    file: UploadFile = File(..., description="包含多序列 DICOM 实例的 ZIP"),
    policy: UploadFile = File(..., description="版本化匿名化策略 JSON 文件"),
) -> JSONResponse:
    zip_bytes = await file.read()
    policy_bytes = await policy.read()
    if not zip_bytes:
        raise HTTPException(status_code=400, detail="file 为空")
    if not policy_bytes:
        raise HTTPException(status_code=400, detail="policy 为空")
    input_sha = hashlib.sha256(zip_bytes).hexdigest()

    job_id = db.new_job_id()
    created = _utc_now()
    db.create_job(job_id, _best_effort_policy_id(policy_bytes), input_sha, created)

    # 暂存原始上传（便于排查；位于数据卷 tmp 下）
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    (TMP_DIR / f"{job_id}.input.zip").write_bytes(zip_bytes)
    (TMP_DIR / f"{job_id}.policy.json").write_bytes(policy_bytes)

    _executor.submit(_process, job_id, zip_bytes, policy_bytes)
    return JSONResponse(
        status_code=202,
        content={
            "job_id": job_id,
            "status": "queued",
            "query_url": f"/jobs/{job_id}",
            "result_url": f"/jobs/{job_id}/result",
            "input_sha256": input_sha,
        },
    )


def _job_view(row: dict) -> dict:
    audit = row.get("audit")
    view = {
        "job_id": row["id"],
        "status": row["status"],
        "policy_id": row["policy_id"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "counts": {
            "instances_in": row["instances_in"],
            "instances_out": row["instances_out"],
            "studies": row["studies"],
            "series": row["series"],
            "errors": row["error_count"],
            "warnings": row["warning_count"],
        },
        "hashes": {
            "input_sha256": row["input_sha256"],
            "output_sha256": row["output_sha256"],
        },
    }
    if audit is not None:
        view["audit"] = audit
    return view


@app.get("/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    row = db.get_job(job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="job 不存在")
    return _job_view(row)


@app.get("/jobs/{job_id}/result")
def get_job_result(job_id: str):
    row = db.get_job(job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="job 不存在")
    if row["status"] in ("queued", "processing"):
        raise HTTPException(status_code=409,
                            detail=f"作业尚未完成（当前状态: {row['status']}）")
    if row["status"] != "completed":
        raise HTTPException(
            status_code=409,
            detail="作业失败，未产出结果文件；请通过 GET /jobs/{id} 查看逐项问题",
        )
    path = ZIP_DIR / f"{job_id}.zip"
    if not path.exists():
        raise HTTPException(status_code=410, detail="结果文件已被清理")
    data = path.read_bytes()
    return Response(
        content=data,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="anon-{job_id}.zip"',
            "X-Content-SHA256": row["output_sha256"] or "",
        },
    )
