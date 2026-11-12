FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TRADING_DATA_DIR=/app/data \
    TRADING_HISTORY_ROOT=/app/data/history \
    TRADING_PAPER_JOURNAL=/app/data/paper/core_paper.db \
    TRADING_ANALYSIS_DB=/app/data/analysis/reports.db
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m pip install . \
    && useradd --create-home --uid 10001 trader \
    && mkdir -p /app/data /app/configs \
    && chown -R trader:trader /app/data /app/configs
COPY --chown=trader:trader configs ./configs
USER trader
EXPOSE 8000
CMD ["crypto-bot", "--host", "0.0.0.0", "--port", "8000"]
