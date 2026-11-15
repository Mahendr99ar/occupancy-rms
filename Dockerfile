FROM python:3.12-slim

WORKDIR /app
# CPU-only torch keeps the image near 1 GB instead of several.
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu
COPY pyproject.toml ./
COPY occupancy ./occupancy
RUN pip install --no-cache-dir .
COPY dashboard ./dashboard
COPY models ./models

ENV MODEL_DIR=/app/models
EXPOSE 8000 8500 8600
CMD ["flask", "--app", "occupancy.api.app", "run", "--host", "0.0.0.0", "--port", "8000"]
