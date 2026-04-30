FROM python:3.14-slim

ARG VERSION=unknown
ENV APP_VERSION=$VERSION

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt && \
    playwright install --with-deps chromium

COPY . .

CMD ["python", "run.py"]