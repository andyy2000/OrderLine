# One image, two commands (see docker-compose.yml):
#   kitchen server:  uvicorn app.server:app --host 0.0.0.0 --port 8000
#   voice bot:       python -m app.voice.bot -t twilio --host 0.0.0.0 --port 7860
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements-voice.txt ./
RUN pip install --no-cache-dir -r requirements-voice.txt

COPY . .
RUN mkdir -p data

EXPOSE 8000 7860
CMD ["uvicorn", "app.server:app", "--host", "0.0.0.0", "--port", "8000"]
