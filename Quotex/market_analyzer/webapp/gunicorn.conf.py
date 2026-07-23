"""Gunicorn production configuration for the Quotex Signal Dashboard.
Usage: gunicorn -c gunicorn.conf.py app:app
(Run from inside Quotex/market_analyzer/webapp/, or via start-prod.sh)
"""
import os

bind = f"0.0.0.0:{os.environ.get('PORT', 5000)}"
workers = int(os.environ.get("WEB_CONCURRENCY", 2))
worker_class = "sync"
timeout = 120
graceful_timeout = 30
keepalive = 5
accesslog = "-"
errorlog = "-"
loglevel = "info"
