# Read-along sidecar generator. GPU: build as-is and run with --gpus all.
# CPU-only: build with --build-arg BASE=python:3.12-slim
ARG BASE=nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04
FROM ${BASE}
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && (command -v python3 >/dev/null || apt-get install -y --no-install-recommends python3 python3-pip) \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN python3 -m pip install --no-cache-dir -r requirements.txt
COPY make_readalong_sidecar.py publish_sidecar.py abs_fetch.py validate_sidecar.py ./
ENV HF_HOME=/models
ENTRYPOINT ["python3", "/app/make_readalong_sidecar.py"]
