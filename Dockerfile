FROM python:3.12-slim

# WeasyPrint system libraries (Pango/HarfBuzz), timezone data and fallback fonts for
# glyphs Raleway lacks (emoji, symbols). Raleway itself is bundled in fonts/.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0 \
      fonts-dejavu-core fonts-noto-color-emoji tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

RUN useradd --uid 1000 --create-home scraper
USER scraper
ENV PYTHONUNBUFFERED=1 TZ=Europe/Berlin DATA_DIR=/data

ENTRYPOINT ["python", "/app/scraper.py"]
