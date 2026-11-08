ARG WEBOX_IMAGE=weward-webox:8ca042c-wx4.1.1.8-compat3
FROM ${WEBOX_IMAGE}
WORKDIR /app
RUN python3 -c "import sqlite3, urllib.request, http.server"
COPY weward /app/weward
COPY docker/collector-entrypoint.sh /app/collector-entrypoint.sh
COPY docker/prepare-storage.sh docker/patch-storage.py /app/
RUN python3 /app/patch-storage.py
RUN chmod +x /app/collector-entrypoint.sh
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 WEBOX_LISTEN_ADDR=127.0.0.1:8080
EXPOSE 8081 6080
ENTRYPOINT ["/usr/bin/tini", "--", "/app/collector-entrypoint.sh"]
