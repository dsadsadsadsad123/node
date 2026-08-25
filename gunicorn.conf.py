import os

# One process keeps VAIGO_NODE_CAPACITY and the in-memory/provider caches global
# for this service. Threads handle health checks and I/O concurrently.
workers = 1
threads = max(4, min(16, int(os.environ.get("VAIGO_GUNICORN_THREADS", "8"))))
timeout = max(15, min(60, int(os.environ.get("VAIGO_GUNICORN_TIMEOUT", "30"))))
graceful_timeout = 12
keepalive = 5
bind = f"0.0.0.0:{os.environ.get('PORT', '10000')}"

# Central already records every node dispatch. Access logging on each worker is
# optional to reduce stdout I/O on small instances.
accesslog = "-" if os.environ.get("VAIGO_ACCESS_LOG", "0").lower() in {"1", "true", "yes", "on"} else None
errorlog = "-"
capture_output = True

# Periodic recycling gives a defensive bound against leaks in third-party HTTP
# stacks without causing frequent cold starts.
max_requests = 1500
max_requests_jitter = 150
