FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DICOM_ANON_DATA_DIR=/data

WORKDIR /app

# Pillow 与 numpy 均有 manylinux wheel，无需编译工具链
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN mkdir -p /data/zips /data/tmp
VOLUME ["/data"]

EXPOSE 8000

# 单进程即可；作业在后台线程池中串行/并行执行，SQLite 位于挂载卷
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
