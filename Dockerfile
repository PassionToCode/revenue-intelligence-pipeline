# the client revenue intelligence — single-stage image, runs as a non-root user.
# `docker run <image>` runs the pipeline, the data-quality gate and all six
# stakeholder answers, so the whole deliverable is demonstrable from the image.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencies first so the layer caches independently of the source.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY specs/ ./specs/
COPY evals/ ./evals/
COPY data/ ./data/

# Non-root: the pipeline only needs to read data/specs and write out/.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/out \
    && chown -R appuser:appuser /app
USER appuser

CMD ["python", "-m", "app", "--demo"]
