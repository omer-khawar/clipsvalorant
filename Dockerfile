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
# This is ffmpeg 5.1.x, which is what bookworm has. vod.py is written to work
# on it: it finds the segment itself and seeks forward inside that segment,
# because 5.1 cannot seek into a lone fragmented-MP4 fragment and writes
# nothing when asked to. Newer builds are more forgiving, so a change here
# will appear to work on a developer machine either way -- read the module
# docstring in vod.py before touching how a frame is pulled.
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

# Dependencies first, resolved from pyproject against a stub package, so this
# layer is keyed on pyproject.toml alone.
#
# This ordering is the whole point. onnxruntime, opencv and numpy are a few
# hundred megabytes of wheels and take about four minutes to install on the
# 1/8-OCPU shape this runs on. Copying the source first -- which is what this
# used to do -- put every one of those minutes behind a one-line edit to a
# Python file, so a typo fix and a dependency bump cost exactly the same.
#
# The stub exists because `pip install .` needs a package to install, and we
# want its dependencies resolved before the real source is anywhere near the
# image. It is overwritten by the COPY below.
#
# yt-dlp from pip rather than apt: Twitch changes its playlist format often
# enough that a distribution's year-old build is a liability.
COPY pyproject.toml ./
RUN mkdir -p clipsvalorant \
 && touch clipsvalorant/__init__.py \
 && pip install --no-cache-dir ".[vision]" yt-dlp

# The source, then the package itself with its dependencies left alone.
# Everything from here down reruns on any code change and costs a second or
# two, which is what a deploy should cost.
COPY clipsvalorant ./clipsvalorant
RUN pip install --no-cache-dir --no-deps --force-reinstall .

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
