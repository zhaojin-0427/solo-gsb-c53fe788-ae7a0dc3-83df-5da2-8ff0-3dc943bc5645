# DICOM 研究数据匿名化 API

基于 **Python 3.11 + FastAPI + SQLite + pydicom + Pillow + NumPy** 实现的 DICOM 研究数据匿名化服务。客户端提交一个包含多序列 DICOM 实例的 ZIP 与一份**版本化匿名化策略**（可带像素遮挡框），服务按 Study / Series / Instance 关系生成**确定性 UID 映射**，清除标准与私有 PHI、在保持跨实例时间差的前提下重写日期，并按遮挡框**真正覆盖像素**后重新编码输出。

> 同一份输入 ZIP + 同一份策略（语义相同，哪怕 JSON 排版不同）永远产出**字节完全一致**的结果 ZIP，可离线复验。

---

## 1. 功能与保证

| 能力 | 说明 |
| --- | --- |
| 层级建模 | 按 `StudyInstanceUID / SeriesInstanceUID / SOPInstanceUID` 建立关系，输出 ZIP 目录结构即新 UID 层级 |
| 确定性 UID | HMAC-SHA256（密钥 = 规范化策略哈希）派生 `2.25.<128bit>` UID；同输入同策略恒定，跨类别（study/series/instance/frame/其他 UI）独立映射；`ReferencedSOPInstanceUID` 等引用随实例一致改写 |
| PHI 清除 | 按 DICOM PS3.15 Basic Application Confidentiality Profile 思路清除患者/机构/人员/设备/自由文本标签；**全部私有标签**（含私有 creator）删除；Overlay(60xx,3000)/Curve(50xx,3000) 像素旁路数据删除；PN 元素清空；写入 `PatientIdentityRemoved=YES` 等去标识化声明 |
| 日期重写 | 每个 Study 用一个确定性平移天数（HMAC 派生，范围由策略给定），实例间相对时间差严格保留；DA/DT 的时间部分、小数秒、时区后缀保留；TM 不动；过早日期按策略钳制 |
| 像素遮挡 | Pillow 生成矩形遮罩、NumPy 直接改写解码后的像素缓冲，支持 8/16 位、单色与交错 RGB/YBR_FULL、按帧遮挡；输出统一重编码为 **Explicit VR Little Endian** 未压缩 |
| 逐项报错，不出半成品 | 缺失引用、非法 VR、压缩像素、策略冲突、目标不存在、重复实例、坏 ZIP/路径逃逸等逐项写入问题清单；出现任意 error 时**不会产出结果 ZIP** |
| 审计与复验哈希 | 结果 ZIP 内含 `AUDIT.json`（UID 映射表、日期平移量、逐实例输入/输出 SHA-256、成员哈希链、动作计数、问题清单）；API 返回结果 ZIP 的 SHA-256，下载响应头 `X-Content-SHA256` 一致 |

### 像素约束

- 仅接受**未压缩**传输语法：Implicit VR Little Endian (`1.2.840.10008.1.2`)、Explicit VR Little Endian (`1.2.840.10008.1.2.1`)、Explicit VR Big Endian (`1.2.840.10008.1.2.2`)。
- JPEG / JPEG 2000 / JPEG-LS / RLE 等压缩实例会被逐项拒绝（`compressed_pixel`），因为遮挡必须真实写回像素；请在提交前自行解压转码。

---

## 2. 一键启动（Docker Compose）

```bash
docker compose up --build
```

服务监听 <http://localhost:8000>，数据（SQLite、结果 ZIP、暂存）保存在命名卷 `dicom-anon-data`（容器内 `/data`）中。

- 健康检查：<http://localhost:8000/health>
- OpenAPI / Swagger UI：<http://localhost:8000/docs>
- ReDoc：<http://localhost:8000/redoc>

停止：

```bash
docker compose down          # 保留数据卷
docker compose down -v       # 连数据卷一起删除
```

### 本地（无 Docker）运行

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

环境变量：

| 变量 | 默认 | 含义 |
| --- | --- | --- |
| `DICOM_ANON_DATA_DIR` | `./data` | 数据目录（SQLite、zips、tmp） |
| `DICOM_ANON_WORKERS` | `2` | 后台作业线程数 |

---

## 3. API

### `POST /jobs`

`multipart/form-data`，两个文件字段：

| 字段 | 内容 |
| --- | --- |
| `file` | DICOM ZIP（成员须为 Part 10 文件，含 `DICM` 标记；允许任意子目录结构） |
| `policy` | 版本化策略 JSON（UTF-8） |

返回 `202 Accepted`：

```json
{
  "job_id": "592134e3fc114c32855ff9e1c9aecb66",
  "status": "queued",
  "query_url": "/jobs/592134e3fc114c32855ff9e1c9aecb66",
  "result_url": "/jobs/592134e3fc114c32855ff9e1c9aecb66/result",
  "input_sha256": "2a5025bb…"
}
```

### `GET /jobs/{id}`

返回状态（`queued / processing / completed / failed`）、计数、输入/输出哈希与完整审计摘要（含逐项 `issues`）。

### `GET /jobs/{id}/result`

- 完成：下载匿名化 ZIP，响应头带 `X-Content-SHA256`；
- 处理中：`409`；失败：`409`（问题清单走 `GET /jobs/{id}`，**失败时没有结果文件**）；
- 不存在：`404`。

---

## 4. 策略文件格式（`policy_version: "1.0"`）

```json
{
  "policy_version": "1.0",
  "policy_id": "research-deid-basic-2026-01",
  "uid_root": "2.25",
  "keep_tags": ["PatientSex"],
  "remove_tags": [],
  "dates": { "shift": true, "jitter_max_days": 180, "min_year": 1901 },
  "occlusions": [
    {
      "study_uid": "1.2.3.400",
      "series_uid": "1.2.3.400.1",
      "sop_instance_uid": null,
      "frames": null,
      "box": { "x": 4, "y": 4, "width": 32, "height": 12, "fill": 0 }
    }
  ]
}
```

字段说明：

- `policy_version`：当前仅支持 `"1.0"`；`policy_id`：调用方自己的版本标识（必填，进入审计）。
- `uid_root`：输出 UID 根，默认 `2.25`（ISO OID 分支 + 128bit 整数，总长 ≤ 64）。可改成机构自有 OID 弧。
- `keep_tags` / `remove_tags`：标签用 DICOM 关键字（`Modality`）或 `(gggg,eeee)`。结构性受保护标签（层级 UID、像素描述、PixelData、传输语法等）与强制 PHI 标签不允许策略干预，冲突逐项报错。
- `dates.shift`：是否平移；`jitter_max_days`：每 Study 确定性抖动的对称范围；`min_year`：平移后钳制下限。
- `occlusions`：遮挡规则，目标逐级收窄（仅 study → study+series → 精确定位 instance；`frames` 省略或为 `null` 表示所有帧）。`box` 坐标为像素列/行，左上角原点，`fill` 为覆盖灰度/分量值。目标不存在或帧号越界均报错。

### 问题码（`issues[].code`）

| code | 含义 |
| --- | --- |
| `policy_invalid` | 策略 JSON/字段非法、未知字段、非法 UID 根、遮挡层级不完整 |
| `policy_conflict` | keep/remove 冲突、keep 强制 PHI、干预受保护标签 |
| `policy_target_missing` | 遮挡规则指向的 study/series/instance 不存在 |
| `zip_invalid` | ZIP 损坏、成员路径逃逸 |
| `dicom_read_error` | 成员不是合法 Part 10 DICOM、空文件、缺层级 UID |
| `illegal_vr` | 非法/不支持的 VR（读取阶段强制全量解码捕获） |
| `compressed_pixel` | 压缩传输语法 |
| `missing_reference` | `ReferencedSOPInstanceUID` 在包内无对应实例 |
| `duplicate_instance` | 同一 SOP Instance UID 重复 |
| `pixel_error` | 像素结构不支持、帧号越界、填充值超界、缓冲长度不符 |
| `date_invalid`（warning） | DA/DT 无法解析，已清空并继续 |

---

## 5. 示例请求

生成示例数据包（3 个实例 / 1 个 Study / 2 个 Series，含 PHI、私有标签、日期和像素白条纹）：

```bash
pip install pydicom pillow numpy
python examples/make_sample_zip.py sample.zip
```

提交并轮询、下载（curl + jq）：

```bash
# 1) 提交
JOB=$(curl -s -X POST http://localhost:8000/jobs \
  -F "file=@sample.zip;type=application/zip" \
  -F "policy=@examples/policy.example.json;type=application/json" \
  | jq -r .job_id)

# 2) 查询状态 / 审计
curl -s http://localhost:8000/jobs/$JOB | jq '.status, .counts, .audit.actions'

# 3) 完成后下载，并核对服务公布的哈希
curl -s -D /tmp/hdr.txt http://localhost:8000/jobs/$JOB/result -o anon.zip
grep -i x-content-sha256 /tmp/hdr.txt
sha256sum anon.zip        # 两个哈希必须一致

# 结果包内容：<newStudyUID>/<newSeriesUID>/<newSOPInstanceUID>.dcm + AUDIT.json
unzip -l anon.zip
```

Python（requests）：

```python
import requests, time

with open("sample.zip", "rb") as fz, open("examples/policy.example.json", "rb") as fp:
    job = requests.post(
        "http://localhost:8000/jobs",
        files={"file": ("sample.zip", fz, "application/zip"),
               "policy": ("policy.json", fp, "application/json")},
    ).json()

while (status := requests.get(f"http://localhost:8000/jobs/{job['job_id']}")
        .json()["status"]) in ("queued", "processing"):
    time.sleep(0.5)

info = requests.get(f"http://localhost:8000/jobs/{job['job_id']}").json()
assert status == "completed", info["audit"]["issues"]

blob = requests.get(f"http://localhost:8000/jobs/{job['job_id']}/result").content
import hashlib
assert hashlib.sha256(blob).hexdigest() == info["hashes"]["output_sha256"]
print("verified:", info["hashes"]["output_sha256"])
```

---

## 6. 可复验性说明（为什么字节一致）

1. 策略先做规范化序列化（键排序、紧凑分隔、UTF-8），再派生 HMAC 密钥；JSON 排版差异不影响结果。
2. UID / 每 Study 日期平移量全部由 HMAC(密钥, 类别+原值) 确定性派生。
3. 结果 ZIP 的成员按新 UID 路径排序，ZIP 条目时间戳固定为 `1980-01-01 00:00:00`、固定 DEFLATE 级别与权限位；`AUDIT.json` 不含作业 ID、真实时钟时间、原始提交字节哈希等任何“每次运行变化”的字段。
4. `AUDIT.json` 中记录：规范化策略哈希、输入 ZIP 哈希、逐成员 SHA-256 及成员哈希链、逐实例新旧 UID、逐实例输出文件 SHA-256、日期平移量与动作计数。复验方用同一策略重跑后比对 ZIP 整体 SHA-256，或解开后逐文件比对即可。

> 失败作业同样在数据库审计中保留完整逐项问题清单（`result=failed`），便于一次性修复后重新提交。

---

## 7. 测试

```bash
pip install -r requirements.txt pytest httpx
python3 -m pytest -q
```

覆盖：确定性与跨作业字节一致、多 Study 日期平移/跨实例时间差、RGB 与多帧逐帧遮挡、引用一致性重写、私有/标准 PHI 清除、压缩像素拒绝、缺失引用、非法 VR、重复实例、策略冲突、目标缺失、坏 ZIP/路径逃逸、400/404/409 语义等（共 35 个用例）。

## 8. 目录结构

```
app/
  main.py        FastAPI 路由与作业调度
  pipeline.py    编排：解析→校验→派生→清洗→遮挡→编码→打包→审计
  deidentify.py  标准/私有 PHI 清除、PN、UID 映射、声明标签
  pixels.py      Pillow+NumPy 像素遮挡（8/16 位、单色/交错彩色、逐帧）
  dates.py       确定性日期平移（DA/DT，保时间差与时区）
  uidmap.py      确定性 UID 派生（HMAC-SHA256，2.25 根）
  schema.py      版本化策略 Pydantic 模型
  issues.py      逐项问题模型
  db.py          SQLite 作业存储（WAL）
  config.py      环境变量配置
examples/        策略示例与样本 ZIP 生成脚本
tests/           端到端与单元测试
docker-compose.yml / Dockerfile / requirements.txt
```
