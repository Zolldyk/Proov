# Proov — Koyeb fallback for when Oracle Cloud A1 is "Out of Capacity".
#
# This is a WORKER image: Proov's only network surface is an OUTBOUND WebSocket to CROO.
# There is no HTTP server and no health endpoint, so there is NO `EXPOSE` and it must be
# deployed as a Koyeb *Worker* service (not Web). Reconnect/heartbeat are SDK-managed and
# the process exits 0 on SIGTERM / 1 on a fatal 1008 — Koyeb's supervisor restarts it, the
# same role systemd plays on the Oracle VM.
#
# ONE KEY, ONE PROCESS: do not run this AND the Oracle systemd unit on the same CROO_API_KEY
# at the same time — the second connection is rejected as `1008`. See the README ops section.
#
# Build:  docker build -t proov .
# Set env vars (CROO_API_URL/CROO_WS_URL/CROO_API_KEY, optional GEMINI_API_KEY/TAVILY_API_KEY)
# in the Koyeb dashboard — never bake secrets into the image.

FROM python:3.12-slim

# No-bytecode + unbuffered stdout so journald/Koyeb logs are line-timely.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Install dependencies via the existing pyproject.toml — no new runtime dep is introduced.
COPY pyproject.toml README.md ./
COPY proov ./proov
RUN pip install --no-cache-dir .

# Run as a non-root user; /app holds the SQLite cache/ledger written to CWD, so it must be
# owned by that user.
RUN useradd --system --no-create-home proov && chown -R proov:proov /app
USER proov

# Worker entrypoint — module form (there is no console-script). SIGTERM → graceful drain → exit 0.
CMD ["python", "-m", "proov"]
