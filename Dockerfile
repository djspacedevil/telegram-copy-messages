FROM python:3.10-slim-bullseye

# System-Abhängigkeiten installieren
RUN apt-get update && apt-get install -y \
    git cmake g++ zlib1g-dev libssl-dev libreadline-dev wget make gperf \
    && rm -rf /var/lib/apt/lists/*

# TDLib bauen (für Container-Architektur)
RUN git clone https://github.com/tdlib/td.git /tdlib && \
    mkdir /tdlib/build && cd /tdlib/build && \
    cmake -DCMAKE_BUILD_TYPE=Release .. && \
    cmake --build . --target tdjson -j$(nproc)

# libtdjson.so in globalen Pfad kopieren und symlink erstellen
RUN cp /tdlib/build/libtdjson.so /usr/lib/libtdjson.so && \
    ln -sf /usr/lib/libtdjson.so /usr/lib/libtdjson.so.1.8.52

# Telegram-Copy-Messages-Projekt einbinden
WORKDIR /app
COPY . .

# Python-Abhängigkeiten installieren
RUN pip install --no-cache-dir -r requirements.txt

# LD_LIBRARY_PATH setzen, damit TDLib gefunden wird
ENV LD_LIBRARY_PATH=/usr/lib:$LD_LIBRARY_PATH

# Container starten
CMD ["python", "main.py"]
