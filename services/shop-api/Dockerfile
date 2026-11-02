FROM python:3.12-slim
RUN pip install --no-cache-dir fastapi==0.115.6 "uvicorn[standard]==0.34.0" prometheus-client==0.21.1 \
      "psycopg[binary]==3.2.3" psycopg-pool==3.2.4 redis==5.2.1
WORKDIR /app
COPY app.py .
EXPOSE 8080
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
