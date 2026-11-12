FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TRADING_HISTORY_ROOT=/app/data/history \
    TRADING_ANALYSIS_DB=/app/data/analysis/reports.db
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m pip install ".[web]" \
    && useradd --create-home --uid 10001 trader \
    && mkdir -p /app/data /app/configs \
    && chown -R trader:trader /app/data /app/configs
COPY --chown=trader:trader configs ./configs
USER trader
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "trading_bot.web.app:app", "--host", "0.0.0.0", "--port", "8000"]
