FROM python:3.12-slim
ENV DEBIAN_FRONTEND=noninteractive
EXPOSE 8000/tcp

WORKDIR /api

COPY requirements.txt .

RUN apt-get update && apt-get install -y \
    libatomic1 && \
    pip install -r requirements.txt && \
    # install chromium browser
    playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*
COPY api.py .

ENTRYPOINT ["python", "api.py"]

