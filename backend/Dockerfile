FROM docker:27-cli AS docker_cli

FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

# docker exec is used only when the optional graph compose override supplies
# the host socket. The default container has no Docker-daemon access.
COPY --from=docker_cli /usr/local/bin/docker /usr/local/bin/docker
COPY pyproject.toml ./
COPY agent/ ./agent/
COPY scripts/ ./scripts/
COPY backend/ ./backend/
RUN pip install --no-cache-dir . \
    && useradd --create-home --uid 1000 fraudloop \
    && mkdir -p /app/data /app/cases /app/HHGOA_IEEE \
    && chown -R fraudloop:fraudloop /app
USER fraudloop
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "backend.app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
