FROM python:3.14-slim

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt && \
    playwright install --with-deps chromium

COPY . .

# Declared last on purpose. VERSION changes on every release, and Docker
# invalidates the layer it lands on plus everything below it — with these two
# lines at the top, each tag rebuilt the 557 MB pip + chromium layer under a new
# digest, so the k3s node re-pulled all of it and the deploy job outran its
# 180s rollout timeout. Below the heavy layer, a version bump only touches this
# one and the node reuses what it already has.
ARG VERSION=unknown
ENV APP_VERSION=$VERSION

CMD ["python", "run.py"]
