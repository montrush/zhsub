FROM python:3.12-slim

# 国内网络构建可覆盖：
#   --build-arg APT_MIRROR=mirrors.163.com --build-arg PIP_INDEX_URL=https://mirrors.163.com/pypi/simple
ARG APT_MIRROR=deb.debian.org
ARG PIP_INDEX_URL=https://pypi.org/simple

ENV TZ=Asia/Shanghai

RUN sed -i "s/deb.debian.org/${APT_MIRROR}/g" /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY zhsub ./zhsub
RUN pip install --no-cache-dir -i ${PIP_INDEX_URL} .

VOLUME ["/config", "/media/strm", "/media/cloud115"]
EXPOSE 8085

ENTRYPOINT ["zhsub"]
# 注意：--config 是全局参数，必须放在子命令之前
CMD ["--config", "/config/config.yaml", "web"]
