FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY holt ./holt
RUN pip install --no-cache-dir .
ENV HOST=0.0.0.0 PORT=8787
EXPOSE 8787
# Configuration comes from environment variables (OTARI_API_KEY, HOLT_ACCESS_CODE, ...), never from a baked-in .env.
CMD ["holt", "serve"]
