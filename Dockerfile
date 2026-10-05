FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# `docker build --target test .` runs the test suite against this image's packages
FROM base AS test
COPY requirements-dev.txt .
RUN pip install --no-cache-dir -r requirements-dev.txt
COPY atvrx ./atvrx
COPY tests ./tests
RUN python -m pytest -q -p no:warnings tests

FROM base AS app
COPY atvrx ./atvrx
RUN useradd --system --uid 10001 atvrx && mkdir -p /recordings /data && chown atvrx /recordings /data
USER atvrx
VOLUME /data
EXPOSE 8095
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8095/healthz', timeout=4)"
CMD ["python", "-m", "atvrx"]
