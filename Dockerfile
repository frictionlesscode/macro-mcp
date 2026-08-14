FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .

ENV SQLITE_PATH=/data/macro.db \
    OAUTH_STATE_PATH=/data/oauth_state.json \
    GARMIN_MCP_OAUTH_STATE_PATH=/data/garmin_mcp_oauth.json \
    TZ=America/New_York \
    LOG_LEVEL=INFO \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8080

EXPOSE 8080

CMD ["python", "-m", "macro_mcp.server"]
