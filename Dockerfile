FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 MCP_HOST=0.0.0.0 MCP_PORT=8767
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . \
    && useradd --uid 10001 --create-home health \
    && mkdir -p /app/data \
    && chown health:health /app/data
USER health
EXPOSE 8767
CMD ["google-health-mcp", "serve", "--transport", "http"]
