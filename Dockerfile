FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY deepsearch/ deepsearch/
RUN pip install --no-cache-dir .
RUN useradd -m appuser && mkdir -p /app/data && chown appuser:appuser /app/data
USER appuser
EXPOSE 8088
CMD ["uvicorn", "deepsearch.api:app", "--host", "0.0.0.0", "--port", "8088"]
