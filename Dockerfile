FROM python:3.12-slim
WORKDIR /app
# Dependencies for compare/cloud_recorder.py (the proxy itself is stdlib only).
COPY compare/requirements.txt /tmp/requirements.txt
# tzdata: time zone data for the controller clock sync (CONTROLLER_TZ).
RUN pip install --no-cache-dir -r /tmp/requirements.txt tzdata && rm /tmp/requirements.txt
COPY proxy/ /app/
COPY compare/ /app/compare/
ENV PYTHONUNBUFFERED=1 LISTEN_PORT=80 LOG_DIR=/logs
VOLUME /logs
EXPOSE 80 8089
# Default: the proxy (with the optional cloud monitor). The standalone cloud
# recorder can also run from the same image with
#   command: ["python3", "/app/compare/cloud_recorder.py"]
CMD ["python3", "/app/estyma_proxy.py"]
