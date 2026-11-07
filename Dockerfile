FROM python:3.12-slim
WORKDIR /app
COPY proxy/ /app/
ENV PYTHONUNBUFFERED=1 LISTEN_PORT=80 LOG_DIR=/logs
VOLUME /logs
EXPOSE 80
CMD ["python3", "/app/estyma_proxy.py"]
