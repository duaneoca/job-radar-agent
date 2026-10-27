# Job Radar Email Agent — the cloud CronJob image (also runnable locally).
#
# Supply chain: every third-party file installed here is hash-pinned. requirements.lock (runtime) and
# requirements-build.lock (setuptools/wheel, builder stage only) are installed with --require-hashes,
# so pip refuses any byte it hasn't seen before. litellm is pinned EXACTLY on purpose — see README
# "Pinned dependencies" before changing it. CI fails the build if site-packages gains an unexpected
# .pth / sitecustomize.py / usercustomize.py / cook.py (scripts/check_site_packages.py).

# ── builder: turn our own source into a wheel, with hash-pinned build tools ──
FROM python:3.12-slim AS builder
WORKDIR /src
COPY requirements-build.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements-build.lock
COPY pyproject.toml README.md ./
COPY agent/ agent/
COPY mcp_email/ mcp_email/
COPY notifications/ notifications/
COPY hitl/ hitl/
RUN pip wheel --no-cache-dir --no-deps --no-build-isolation -w /dist .

# ── runtime: locked deps (hash-checked) + our wheel; no build tools ──
FROM python:3.12-slim
WORKDIR /app
COPY requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock
COPY --from=builder /dist/ /dist/
RUN pip install --no-cache-dir --no-deps --no-index /dist/*.whl && rm -rf /dist

# Default: local interval loop. The cloud k8s CronJob overrides CMD with ["job-radar-agent","cloud"].
CMD ["job-radar-agent", "run"]
