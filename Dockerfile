FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt pytest

COPY . .

EXPOSE 8000
CMD ["python", "docker/server.py"]
