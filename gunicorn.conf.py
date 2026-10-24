# Loaded automatically by gunicorn. A refresh calls two government feeds and can
# take longer than gunicorn's 30 second default, which would kill the worker.
timeout = 120
