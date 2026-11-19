# The image the hosted page runs in. Nothing here is needed to run the tool
# locally -- `python -m clipsvalorant serve` on a checkout is still the whole
# install -- but a container has to be told about the parts of this project
# that are not Python.
#
# Pinned to bookworm rather than tracking `slim`: Debian's 64-bit time_t
# transition renamed a shelf of library packages, and the two this needs are
# spelled differently on either side of it. 3.12 rather than the newest Python
# for the same reason - onnxruntime and rapidocr ship wheels for it today.
FROM python:3.12-slim-bookworm

# The two programs Layer 5 shells out to, and the libraries OpenCV loads at
# import time.
#
# ffmpeg and yt-dlp are what make a verdict better than a guess: without them
# on PATH the resolver still finds broadcasts and still ranks them, but it can
# never open one to read the ten names off the screen, so every candidate
# stops at "likely". vision.py catches the ImportError and reports itself
# unavailable, which means forgetting libgl1 here costs you confirmation
# silently rather than loudly -- hence installing it even though the image
# builds fine without it.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ffmpeg \
      ca-certificates \
      libgl1 \
      libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY pyproject.toml ./
COPY clipsvalorant ./clipsvalorant

# yt-dlp from pip rather than apt: Twitch changes its playlist format often
# enough that a distribution's year-old build is a liability.
RUN pip install --no-cache-dir ".[vision]" yt-dlp

# Everything that is state lives under one directory, so one mounted volume
# covers all of it: the per-account databases, the artwork cache and the
# frames pulled out of VODs.
ENV DB_PATH=/data/clipsvalorant.db \
    PORT=8080

EXPOSE 8080

# 0.0.0.0 because the host's proxy is a different machine from this one, and
# --no-open because there is no browser in here to open. CLIPS_PASSWORD is
# what keeps that first argument from being a mistake; set it as a secret.
CMD ["sh", "-c", "exec python -m clipsvalorant serve --host 0.0.0.0 --port ${PORT:-8080} --no-open"]
