FROM python:3.12-slim

# fonts-dejavu-core: axtarışlı PDF-də ə hərfi üçün Unicode şrift
# libreoffice-writer + fonts-liberation: DOCX-dən axtarışlı PDF üçün (DOCX PDF-ə çevrilir)
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-dejavu-core fonts-liberation libreoffice-writer \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

COPY backend backend
COPY frontend frontend

WORKDIR /app/backend
ENV PORT=8000
EXPOSE 8000
CMD uvicorn main:app --host 0.0.0.0 --port ${PORT}
