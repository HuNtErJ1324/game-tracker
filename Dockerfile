# Game Tracker server (dashboard + database + Claude coach) for a home server.
# The tracker agent still runs on your Windows gaming PC and reports here.
FROM python:3.13-slim
RUN apt-get update && apt-get install -y --no-install-recommends tzdata && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt uv  # uv: prime-agent builds its Python kernel with it
COPY gametracker ./gametracker
ENV GAMETRACKER_DATA=/data PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8787
CMD ["python", "-m", "gametracker", "server", "--host", "0.0.0.0", "--port", "8787"]
