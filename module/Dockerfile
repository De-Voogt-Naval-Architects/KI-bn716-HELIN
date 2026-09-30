# Stage 1 - export poetry lockfile to the requirements.txt
FROM --platform=$BUILDPLATFORM python:3.12-slim AS deps
WORKDIR /app
RUN pip install --no-cache-dir poetry==2.1.3 poetry-plugin-export==1.9.0
COPY pyproject.toml poetry.lock ./
RUN poetry export --only main --without-hashes --output requirements.txt

# Stage 2 - pip install from the requirements.txt
FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
COPY --from=deps /app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY extract.py destinations.py helin_status.py Trending_pb2.py Trending_pb2_grpc.py ./
COPY signals ./signals

# Run as non-root (uid 1000). /data/out is where the CSVs go: mount a named
# volume, or a host folder owned by 1000:1000.
RUN useradd --create-home --uid 1000 module \
    && mkdir -p /data/out && chown -R module:module /data
USER module
VOLUME ["/data/out"]

# Flags from the node template's Cmd are appended to the entrypoint. With none,
# it runs in follow mode from DL_* environment variables and the bundled bn716
# signal list.
ENTRYPOINT ["python", "/app/extract.py"]
CMD []
