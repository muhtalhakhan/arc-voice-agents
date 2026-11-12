FROM python:3.12-slim AS build
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.12-slim
WORKDIR /app
RUN useradd --create-home --uid 10001 app
COPY --from=build /install /usr/local
COPY agents ./agents
USER app
ENV PORT=8080 AGENTS_DIR=/app/agents PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["sh", "-c", "uvicorn arcvoice.api:main --factory --host 0.0.0.0 --port ${PORT}"]
