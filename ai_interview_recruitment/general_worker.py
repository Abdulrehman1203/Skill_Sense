"""Explicit general-queue subscription; never import/subscribe to vision models."""
import os


def main():
    if os.name == 'nt':
        raise SystemExit('Production Celery requires the separately verified Linux process model; Windows is test-only.')
    from .celery import app
    app.worker_main(['worker', '-Q', 'celery', '--pool=prefork', '--concurrency=2',
        '--prefetch-multiplier=1', '--loglevel=WARNING', '--hostname=general@%h',
        '--without-gossip', '--without-mingle'])


if __name__ == '__main__':
    main()
