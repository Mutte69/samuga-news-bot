FROM python:3.11-slim
WORKDIR /app
RUN apt-get update && apt-get install -y \
    fonts-dejavu-core \
    fonts-noto \
    libgirepository1.0-dev \
    libcairo2-dev \
    pkg-config \
    python3-dev \
    gir1.2-pango-1.0 \
    libpango1.0-dev \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install --no-cache-dir PyGObject pycairo
COPY . .
CMD ["python", "bot.py"]
