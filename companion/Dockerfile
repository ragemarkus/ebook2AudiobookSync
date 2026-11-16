# Read-along companion (XPS, CPU) and remote worker (G14, GPU) image.
# Build from the repository root so the existing sidecar/ tools are included:
#   docker build -f companion/Dockerfile -t readalong-companion:cpu .
#   docker build -f companion/Dockerfile --build-arg BASE=nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04 \
#       -t readalong-companion:gpu .
ARG BASE=python:3.12-slim
FROM ${BASE}
ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
 && (command -v python3 >/dev/null && python3 -m pip --version >/dev/null 2>&1 \
     || apt-get install -y --no-install-recommends python3 python3-pip) \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY sidecar/requirements.txt /app/sidecar/requirements.txt
RUN python3 -m pip install --no-cache-dir -r /app/sidecar/requirements.txt
# the existing generator / validator / publisher, unchanged
COPY sidecar/make_readalong_sidecar.py sidecar/validate_sidecar.py sidecar/publish_sidecar.py sidecar/abs_fetch.py /app/sidecar/
COPY companion/readalong_companion /app/readalong_companion
ENV SIDECAR_DIR=/app/sidecar DATA_DIR=/data HF_HOME=/data/models PORT=8765
VOLUME ["/data"]
EXPOSE 8765
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
  CMD python3 -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:%s/api/health'%os.environ.get('PORT','8765'),timeout=4)" || exit 1
ENTRYPOINT ["python3", "-m", "readalong_companion"]
CMD ["serve"]
