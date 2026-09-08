FROM python:3.12-slim

# ffmpeg is required for thumbnails and (later) -c copy trims. This is the
# ONLY reason the image is not python:3.12-alpine — ffmpeg on Alpine is a
# fight not worth having for ~40MB.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml ./
COPY clipd ./clipd
RUN pip install --no-cache-dir .

EXPOSE 8000
CMD ["uvicorn", "clipd.asgi:app", "--host", "0.0.0.0", "--port", "8000"]
