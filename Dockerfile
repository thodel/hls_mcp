FROM python:3.12-slim

WORKDIR /app
COPY build_db.py db.py embed_db.py embeddings.py server.py requirements.txt ./

# Install deps, then delete build tools
RUN pip install --no-cache-dir -r requirements.txt

# Default: build DB from source CSV if db does not exist
# Override HLS_SRC_CSV / HLS_OUT_DB in docker-compose if your CSV is elsewhere.
CMD ["python", "server.py", "--db", "/data/hls.db", "--host", "0.0.0.0", "--port", "8004"]
