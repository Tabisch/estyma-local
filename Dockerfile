FROM python:3.12-slim
WORKDIR /app
# Python packages from requirements.txt: the EstymaApi wrapper for the cloud
# monitoring, paho-mqtt and tzdata (the server itself is stdlib only).
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && rm /tmp/requirements.txt
COPY estyma_local/ /app/estyma_local/
ENV PYTHONUNBUFFERED=1 LISTEN_PORT=80 LOG_DIR=/logs
VOLUME /logs
EXPOSE 80 8089
# Default: the server (proxy / local server, web UI, optional cloud
# monitoring and MQTT). The analysis tools run from the same image, e.g.
#   docker exec <container> python3 -m estyma_local.analysis.summarize_logs /logs
CMD ["python3", "-m", "estyma_local"]
