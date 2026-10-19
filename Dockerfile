FROM python:3.12-slim
WORKDIR /app
RUN useradd --uid 10001 --create-home machine && mkdir /data && chown machine:machine /data
COPY server.py core.py archive_tools.py /app/
COPY web /app/web
USER machine
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
EXPOSE 8780
CMD ["python", "server.py", "--data", "/data", "--host", "0.0.0.0"]
