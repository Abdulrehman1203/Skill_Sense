import base64
import hashlib
import shutil
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

import boto3
from botocore.config import Config
from django.conf import settings
from django.db import transaction
from django.db.models import Sum

from ..models import InterviewReplay, ReplayBudget, ReplayObject

PREFIX = 'interview-replay/'
MAX_CHUNK = 2 * 1024 * 1024
MAX_CAPTURE = 60 * 60
MAX_INPUT = 320 * 1024 * 1024
MAX_AUDIO = 384 * 1024 * 1024
MAX_OUTPUT = 320 * 1024 * 1024
MAX_SEGMENTS = 16
PLAYBACK_TTL = 180


class QuotaExceeded(Exception):
    pass


class InvalidChunk(ValueError):
    pass


def configured():
    if not all((settings.REPLAY_R2_ENDPOINT, settings.REPLAY_R2_BUCKET,
                settings.REPLAY_R2_ACCESS_KEY, settings.REPLAY_R2_SECRET_KEY)):
        return False
    try:
        endpoint = urlsplit(settings.REPLAY_R2_ENDPOINT)
        return bool(endpoint.scheme == 'https' and endpoint.hostname and endpoint.port in (None, 443)
                    and not endpoint.username and not endpoint.password and endpoint.path in ('', '/')
                    and not endpoint.query and not endpoint.fragment)
    except ValueError:
        return False


def client():
    if not configured():
        raise ValueError('Private replay storage is not configured.')
    return boto3.client(
        's3', endpoint_url=settings.REPLAY_R2_ENDPOINT, region_name='auto',
        aws_access_key_id=settings.REPLAY_R2_ACCESS_KEY,
        aws_secret_access_key=settings.REPLAY_R2_SECRET_KEY,
        config=Config(signature_version='s3v4', connect_timeout=3, read_timeout=8,
                      retries={'total_max_attempts': 1}, request_checksum_calculation='when_required',
                      response_checksum_validation='when_required'),
    )


@contextmanager
def budget():
    with transaction.atomic():
        ReplayBudget.objects.get_or_create(pk=1)
        yield ReplayBudget.objects.select_for_update().get(pk=1)


def reserve(ledger, size):
    objects = ReplayObject.objects.aggregate(total=Sum('size'))['total'] or 0
    scratch = InterviewReplay.objects.aggregate(total=Sum('scratch_bytes'))['total'] or 0
    if not ledger.inventory_ok or objects + scratch + size > settings.REPLAY_BUDGET_BYTES:
        raise QuotaExceeded


def put_chunk(s3, obj, data):
    s3.put_object(Bucket=settings.REPLAY_R2_BUCKET, Key=obj.key, Body=data,
                  ContentLength=len(data), ContentType='application/octet-stream',
                  ContentMD5=base64.b64encode(hashlib.md5(data, usedforsecurity=False).digest()).decode(),
                  Metadata={'sha256': obj.digest})


def copy_chunk(s3, obj, destination):
    response = s3.get_object(Bucket=settings.REPLAY_R2_BUCKET, Key=obj.key)
    with response['Body'] as body:
        if response['ContentLength'] != obj.size or response.get('Metadata', {}).get('sha256') != obj.digest:
            raise InvalidChunk('Chunk metadata mismatch.')
        data = body.read(MAX_CHUNK + 1)
    if len(data) != obj.size or hashlib.sha256(data).hexdigest() != obj.digest:
        raise InvalidChunk('Chunk verification failed.')
    destination.write(data)


def put_output(s3, key, source, size, digest):
    with source.open('rb') as media:
        checksum = hashlib.file_digest(media, lambda: hashlib.md5(usedforsecurity=False)).digest()
    with source.open('rb') as media:
        s3.put_object(Bucket=settings.REPLAY_R2_BUCKET, Key=key, Body=media,
                      ContentLength=size, ContentType='video/mp4',
                      ContentMD5=base64.b64encode(checksum).decode(),
                      CacheControl='private, no-store', Metadata={'sha256': digest})
    head = s3.head_object(Bucket=settings.REPLAY_R2_BUCKET, Key=key)
    if head['ContentLength'] != size or head.get('Metadata', {}).get('sha256') != digest:
        raise ValueError('Replay upload verification failed.')
    response = s3.get_object(Bucket=settings.REPLAY_R2_BUCKET, Key=key, Range='bytes=0-15')
    with response['Body'] as body, source.open('rb') as local:
        if (response['ResponseMetadata']['HTTPStatusCode'] != 206
                or response.get('ContentRange') != f'bytes 0-15/{size}'
                or body.read(16) != local.read(16)):
            raise ValueError('Replay byte range verification failed.')


def signed_url(key, seconds):
    return client().generate_presigned_url(
        'get_object', Params={'Bucket': settings.REPLAY_R2_BUCKET, 'Key': key}, ExpiresIn=seconds,
    )


def scratch_space():
    return tempfile.TemporaryDirectory(prefix='skillsense-replay-', dir=settings.REPLAY_SCRATCH_DIRECTORY)


def clean_stale_scratch(max_age):
    # Operate only in the private worker volume. Temporary files on a killed
    # Celery child survive until that child's execution and subprocess limits end.
    if not settings.REPLAY_SCRATCH_DIRECTORY:
        return
    cutoff = time.time() - max_age
    for directory in Path(settings.REPLAY_SCRATCH_DIRECTORY).glob('skillsense-replay-*'):
        try:
            if not directory.is_symlink() and directory.is_dir() and directory.stat().st_mtime < cutoff:
                shutil.rmtree(directory)
        except FileNotFoundError:
            continue
