# Pinned by digest. The 3.14-slim tag is republished every few days, and each new
# base digest invalidates every layer on top of it: release builds then miss the
# cache warm-build-cache.yml left on master, rebuild the 557 MB pip + chromium
# layer, and the k3s node re-pulls the whole 600 MB image (7m13s at v1.12.1).
# Dependabot (.github/dependabot.yml) moves the digest in a reviewable PR, and
# merging it re-warms the cache, since that workflow runs on Dockerfile changes.
FROM python:3.14-slim@sha256:f85c5697265c178cc6887276c55fe16cf3d14ca35c3df6a5eab3b360534a55d2

WORKDIR /app

# requirements.lock, not requirements.txt: the direct pins in requirements.txt
# leave every transitive dependency free to resolve to whatever is newest at
# build time, so two images built from the same commit could ship different
# libraries. The lock names all of them.
COPY requirements.lock .

RUN pip install --no-cache-dir -r requirements.lock && \
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
