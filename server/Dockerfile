FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py ./
COPY templates ./templates
COPY static ./static
RUN useradd -r app && mkdir -p /data && chown -R app /app /data
USER app
EXPOSE 8000
CMD ["python", "server.py"]
