"""Local schema rollout with worker check and verified PostgreSQL backup first."""
from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
import subprocess

from django.conf import settings
from django.core.management import BaseCommand, CommandError, call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor


class Command(BaseCommand):
    help = 'Back up PostgreSQL and apply migrations with drained workers/clients. Does not start services.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true')

    def handle(self, *args, **options):
        import json
        executor = MigrationExecutor(connection)
        plan = executor.migration_plan(executor.loader.graph.leaf_nodes())
        if not options['apply']:
            self.stdout.write(json.dumps({'pending': [m.name for m, backward in plan], 'applied': False}))
            return
        if not plan:
            self.stdout.write(json.dumps({'pending': [], 'applied': True, 'changed': False}))
            return
        from ai_interview_recruitment.celery import app
        try:
            workers = app.control.inspect(timeout=1).ping() or {}
        except Exception:
            raise CommandError('Cannot verify worker drainage while the broker is unavailable.') from None
        if workers:
            raise CommandError('Stop/drain existing workers before schema rollout; responding workers remain.')
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid() AND backend_type='client backend'")
            if cursor.fetchone()[0]:
                raise CommandError('Other database clients are connected. Stop web/producers and retry after drainage.')
        binary = shutil.which('pg_dump')
        if not binary:
            raise CommandError('Install pg_dump or put it on PATH before applying migrations.')
        target = Path(settings.BASE_DIR) / 'backups'
        target.mkdir(exist_ok=True)
        backup = target / ('workflow-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '.dump')
        config = settings.DATABASES['default']
        env = os.environ.copy()
        env['PGPASSWORD'] = str(config.get('PASSWORD', ''))
        args = [binary, '--format=custom', '--no-owner', '--no-acl', '--file', str(backup),
            '--host', str(config.get('HOST') or 'localhost'), '--port', str(config.get('PORT') or 5432),
            '--username', str(config['USER']), str(config['NAME'])]
        try:
            subprocess.run(args, env=env, check=True, capture_output=True, timeout=120)
            restore = str(Path(binary).with_name('pg_restore.exe' if os.name == 'nt' else 'pg_restore'))
            subprocess.run([restore, '--list', str(backup)], check=True, capture_output=True, timeout=30)
            if backup.stat().st_size < 5:
                raise ValueError()
        except Exception:
            raise CommandError('Database backup verification failed. No migrations were applied; inspect local PostgreSQL tooling.') from None
        call_command('migrate', interactive=False, stdout=self.stdout)
        self.stdout.write(json.dumps({'applied': True, 'backup': str(backup),
            'backup_archive_verified': True, 'restore_rehearsed': False, 'services_started': False}))
