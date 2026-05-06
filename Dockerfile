FROM python:3.12-slim
RUN pip install --no-cache-dir oci requests
COPY monitor.py /app/monitor.py
CMD ["python", "/app/monitor.py"]
