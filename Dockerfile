FROM python:3.12-slim
WORKDIR /app
# Python packages from requirements.txt: the EstymaApi wrapper for the cloud
# monitoring, paho-mqtt and tzdata (the server itself is stdlib only).
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && rm /tmp/requirements.txt \
 && useradd --uid 1000 --user-group --no-create-home --shell /usr/sbin/nologin estyma \
 && mkdir -p /logs && chown estyma:estyma /logs
COPY estyma_local/ /app/estyma_local/
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
ENV PYTHONUNBUFFERED=1 LISTEN_PORT=80 LOG_DIR=/logs
VOLUME /logs
EXPOSE 80 8089
# The entrypoint fixes the ownership of an existing log volume and then runs
# the command as the unprivileged user "estyma" (see docker-entrypoint.sh).
ENTRYPOINT ["docker-entrypoint.sh"]
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
  CMD ["python3", "-m", "estyma_local.healthcheck"]
# Default: the server (proxy / local server, web UI, optional cloud
# monitoring and MQTT). The analysis tools run from the same image, e.g.
#   docker exec <container> python3 -m estyma_local.analysis.summarize_logs /logs
CMD ["python3", "-m", "estyma_local"]
