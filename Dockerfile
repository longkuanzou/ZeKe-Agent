# P7 交付镜像：API 与 legacy 演示服务共用同一个镜像，由 compose 覆盖启动命令。
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 只依赖 wheel；不在构建期 apt-get（部分内网/离线镜像无法访问 Debian 源）。
# 构建源可通过 --build-arg PIP_INDEX_URL=... 覆盖为企业内网镜像。
ARG PIP_INDEX_URL=https://pypi.org/simple

COPY pyproject.toml README.md ./
COPY apps ./apps
COPY zkagent ./zkagent
COPY scripts ./scripts

RUN pip install --upgrade pip --index-url "$PIP_INDEX_URL" \
    && pip install --index-url "$PIP_INDEX_URL" .

EXPOSE 8000 8100

CMD ["uvicorn", "apps.api:app", "--host", "0.0.0.0", "--port", "8000"]
