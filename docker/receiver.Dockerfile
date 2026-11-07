FROM python:3.11-slim-bookworm@sha256:a83ba18aef0f865b1a4a611c2cf460fe446f2795673bbb9fe43be570439e3b39
WORKDIR /app
COPY weward/__init__.py weward/common.py weward/receiver.py weward/relay.py weward/schedule.py weward/notifications.py /app/weward/
COPY weward/static /app/weward/static
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
EXPOSE 8080
CMD ["python3", "-m", "weward.receiver"]
