# ===========================================================================
# AgentOS
# ===========================================================================
FROM agnohq/python:3.14

# ---------------------------------------------------------------------------
# Application code
# ---------------------------------------------------------------------------
WORKDIR /app
ENV PYTHONPATH=/app
COPY requirements.txt ./
RUN uv pip sync requirements.txt --system
COPY . .

# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
RUN chmod +x /app/scripts/entrypoint.sh /app/scripts/shim/uvicorn
# Railway runs a startCommand INSTEAD of the ENTRYPOINT. The shim directory is FIRST on PATH so a bare
# `uvicorn ...` start still runs the workspace boot and the token scrub (scripts/shim/uvicorn).
ENV PATH="/app/scripts/shim:${PATH}"
ENTRYPOINT ["/app/scripts/entrypoint.sh"]

# ---------------------------------------------------------------------------
# Default command (overridden by compose for dev)
# ---------------------------------------------------------------------------
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
