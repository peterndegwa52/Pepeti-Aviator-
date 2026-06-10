#!/bin/bash
# Pepeti Aviator startup script
python -c "from app import app, init_db; init_db(); print('DB initialized')"
gunicorn --worker-class eventlet -w 1 --bind 0.0.0.0:${PORT:-5000} app:app
