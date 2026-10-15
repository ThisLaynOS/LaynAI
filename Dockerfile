FROM python:3.14-slim-bookworm
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src
WORKDIR /app
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin bot
COPY requirements.lock .
RUN pip install --no-cache-dir -r requirements.lock
COPY --chown=bot:bot src ./src
COPY --chown=bot:bot scripts ./scripts
RUN mkdir /app/data && chown bot:bot /app/data
USER bot
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz')"
CMD ["python", "src/main.py"]
