# Job Radar Email Agent — local self-host image (also the base for the cloud CronJob).
FROM python:3.12-slim

WORKDIR /app

# deps first for layer caching
COPY agent/requirements.txt agent/requirements.txt
COPY mcp_email/requirements.txt mcp_email/requirements.txt
RUN pip install --no-cache-dir -r agent/requirements.txt -r mcp_email/requirements.txt

COPY agent/ agent/
COPY mcp_email/ mcp_email/
COPY notifications/ notifications/
COPY hitl/ hitl/
COPY prompts/ prompts/
COPY scripts/ scripts/

# default: the interval scheduler (local path). Cloud CronJob overrides CMD with --once.
CMD ["python", "scripts/run_loop.py"]
