FROM python:3.11-slim

# Avoid writing .pyc files & buffer output so we don't drop python logs
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

WORKDIR /app

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Launch the HFT Scalper engine
CMD ["python", "hft_scalper.py"]
