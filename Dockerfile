FROM python:3.12-slim

WORKDIR /app

# mediapipe (progress-photo pose alignment -- pyproject.toml's "photos" extra) wraps OpenCV,
# which dynamically links these at import time even though pip never installs them itself --
# a Debian-slim base has none of them by default. curl fetches the pose model below.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 libglib2.0-0 libsm6 libxext6 libxrender1 curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY src ./src

# Pose alignment is a real ML dependency (~200MB+) without a working wheel on every
# architecture (some ARM boards in particular) -- see body_photos.py's module docstring for
# how it degrades (unaligned photos, not a broken server) when it's missing. Default on;
# rebuild with --build-arg PHOTO_EXTRAS= (empty) on a host where installing it fails.
ARG PHOTO_EXTRAS=[photos]
RUN pip install --no-cache-dir ".${PHOTO_EXTRAS}"

# mediapipe's classic bundled-weights Pose API was removed as of mediapipe 1.x (verified
# against the actual installed package -- see body_photos.py's module docstring); the
# remaining Tasks API needs a real model file, so it's fetched here at build time instead of
# at runtime -- the running container should never need outbound internet access for this.
# Skipped when PHOTO_EXTRAS is empty (mediapipe itself wasn't installed).
RUN if [ -n "${PHOTO_EXTRAS}" ]; then \
      mkdir -p /app/models && \
      curl -fsSL -o /app/models/pose_landmarker_lite.task \
        https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_lite/float16/latest/pose_landmarker_lite.task ; \
    fi

ENV SQLITE_PATH=/data/macro.db \
    OAUTH_STATE_PATH=/data/oauth_state.json \
    GARMIN_MCP_OAUTH_STATE_PATH=/data/garmin_mcp_oauth.json \
    POSE_MODEL_PATH=/app/models/pose_landmarker_lite.task \
    TZ=America/New_York \
    LOG_LEVEL=INFO \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8080

EXPOSE 8080

CMD ["python", "-m", "macro_mcp.server"]
