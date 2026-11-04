FROM python:3.12-slim
RUN pip install --no-cache-dir fastapi==0.115.6 "uvicorn[standard]==0.34.0" \
      opentelemetry-sdk==1.29.0 opentelemetry-exporter-otlp-proto-http==1.29.0 \
      opentelemetry-instrumentation-fastapi==0.50b0
WORKDIR /app
COPY app.py .
EXPOSE 8081
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8081", "--workers", "1"]
