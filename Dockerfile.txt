FROM python:3.13-slim
WORKDIR /app
RUN apt-get update && apt-get install -y \
    fonts-dejavu-core \
    fonts-noto \
    python3-gi \
    python3-gi-cairo \
    gir1.2-pango-1.0 \
    libpango1.0-dev \
    libcairo2-dev \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["python", "bot.py"]
