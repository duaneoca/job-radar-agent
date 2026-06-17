# Job Radar Email Agent — the cloud CronJob image (also runnable locally).
FROM python:3.12-slim

WORKDIR /app

# Install the package (pulls deps from pyproject; seed prompts ship as package data).
COPY pyproject.toml README.md ./
COPY agent/ agent/
COPY mcp_email/ mcp_email/
COPY notifications/ notifications/
COPY hitl/ hitl/
RUN pip install --no-cache-dir .

# Default: local interval loop. The cloud k8s CronJob overrides CMD with ["job-radar-agent","cloud"].
CMD ["job-radar-agent", "run"]
