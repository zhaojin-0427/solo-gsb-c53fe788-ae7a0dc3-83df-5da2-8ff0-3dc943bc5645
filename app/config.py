"""运行期配置：所有路径均可通过环境变量覆盖，方便容器化部署。"""

import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("DICOM_ANON_DATA_DIR", "./data")).resolve()
ZIP_DIR = DATA_DIR / "zips"          # 成功作业的匿名化 ZIP
TMP_DIR = DATA_DIR / "tmp"           # 上传暂存与处理暂存目录
DB_PATH = DATA_DIR / "jobs.db"

# 策略中不写 uid_root 时使用的默认 UID 根（2.25 == ISO OID 分支，后跟整数）
DEFAULT_UID_ROOT = "2.25"

# 日期平移允许的最大抖动天数（防御性上限，策略文件里可以更小）
MAX_DATE_JITTER_DAYS = 3650

def ensure_dirs() -> None:
    for d in (DATA_DIR, ZIP_DIR, TMP_DIR):
        d.mkdir(parents=True, exist_ok=True)
