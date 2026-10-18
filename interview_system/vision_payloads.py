"""Private transient Redis payloads. Never use application cache or task arguments.

Dedicated non-persistent Redis primary required in production (no snapshots/AOF/
replication backups). ACL/network/TLS policy is an operator responsibility.
"""
import math
import logging

from django.conf import settings
from django.utils import timezone
from redis import Redis
from redis.backoff import NoBackoff
from redis.retry import Retry
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)


class PayloadCapacity(Exception):
    pass


STORE = """
local now = redis.call('TIME'); now = tonumber(now[1]) + tonumber(now[2])/1000000
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[1]) then return 0 end
local ttl = tonumber(ARGV[2])
redis.call('SET', KEYS[2], ARGV[3], 'PX', ttl, 'NX')
redis.call('ZADD', KEYS[1], now+ttl/1000, KEYS[2])
if redis.call('PTTL', KEYS[1]) < ttl+1000 then redis.call('PEXPIRE', KEYS[1], ttl+1000) end
return 1
"""


class PayloadStore:
    def __init__(self):
        self.client = Redis.from_url(settings.INTERVIEW_ANALYSIS_PAYLOAD_REDIS_URL,
            socket_timeout=0.4, socket_connect_timeout=0.4, retry=Retry(NoBackoff(), 0))
        self.prefix = settings.INTERVIEW_ANALYSIS_PAYLOAD_PREFIX

    def key(self, job_id):
        return f'{self.prefix}:image:{job_id}'

    def put(self, job, data):
        ttl = math.floor((job.expires_at-timezone.now()).total_seconds()*1000)
        if ttl <= 0 or len(data) > settings.INTERVIEW_ANALYSIS_MAX_JPEG_BYTES:
            raise PayloadCapacity()
        if not self.client.eval(STORE, 2, self.prefix+':slots', self.key(job.pk),
                settings.INTERVIEW_ANALYSIS_PAYLOAD_CAP, ttl, data):
            raise PayloadCapacity()

    def get(self, job_id):
        return self.client.get(self.key(job_id))

    def delete(self, job_id):
        # Atomic removal; failure only leaves a bounded TTL orphan.
        try:
            self.client.eval("redis.call('DEL', KEYS[2]); return redis.call('ZREM', KEYS[1], KEYS[2])",
                             2, self.prefix+':slots', self.key(job_id))
        except RedisError:
            # Fixed, credential-free event for operator log counters. Redis may
            # itself be unavailable, so a Redis-only failure counter is unsafe.
            logger.warning('InterviewAnalysis payload cleanup deferred; bounded TTL remains authoritative.')
            raise

    def close(self):
        self.client.close()
