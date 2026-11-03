# Pinned to an exact Python release and image digest: the image only changes when we choose to.
FROM python:3.13.18-slim@sha256:ae14d4ce8771606477a81446e7ef429edd6762305fc50c84507c1041fa8285d1

LABEL org.opencontainers.image.title="Nextarr" \
      org.opencontainers.image.description="Rolling TV library for Sonarr: keep only the episodes people are about to watch" \
      org.opencontainers.image.licenses="MIT" \
      com.centurylinklabs.watchtower.enable="false"

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 NEXTARR_CONFIG_DIR=/config NEXTARR_PORT=8790
WORKDIR /app
COPY nextarr /app/nextarr
RUN useradd --uid 1000 --create-home nextarr && mkdir -p /config && chown nextarr /config
USER 1000
VOLUME /config
EXPOSE 8790
HEALTHCHECK --interval=5m --timeout=15s --start-period=3m --start-interval=15s --retries=3 \
  CMD ["python", "-m", "nextarr.healthcheck"]
CMD ["python", "-m", "nextarr"]
