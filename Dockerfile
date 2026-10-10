FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY zhsub ./zhsub
RUN pip install --no-cache-dir .

VOLUME ["/config", "/media/strm", "/media/cloud115"]
EXPOSE 8085

ENTRYPOINT ["zhsub"]
CMD ["web", "--config", "/config/config.yaml"]
