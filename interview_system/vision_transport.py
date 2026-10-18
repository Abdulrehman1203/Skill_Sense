"""Pinned Kombu Redis extension: bound BOTH publication and broker restoration.

Only the dedicated vision list is changed. Overflow redelivery may be discarded:
the PostgreSQL ledger, not Redis delivery, owns recovery. Never log message bodies.
"""
from django.conf import settings
from kombu.transport.redis import Channel as RedisChannel, Transport as RedisTransport
from kombu.utils.json import dumps

BOUNDED_PUSH = """
if redis.call('LLEN', KEYS[1]) >= tonumber(ARGV[1]) then return 0 end
redis.call(ARGV[4], KEYS[1], ARGV[2])
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[3]))
return 1
"""


class VisionQueueFull(Exception):
    pass


class Channel(RedisChannel):
    def _put(self, queue, message, **kwargs):
        if queue != settings.INTERVIEW_ANALYSIS_VISION_QUEUE:
            return super()._put(queue, message, **kwargs)
        if self._get_message_priority(message, reverse=False) != 0:
            raise ValueError('Vision priority must be zero.')
        encoded = dumps(message)
        if len(encoded.encode('utf-8')) > 8192:
            raise ValueError('Vision message exceeds metadata limit.')
        with self.conn_or_acquire() as client:
            if not client.eval(BOUNDED_PUSH, 1, queue, settings.INTERVIEW_ANALYSIS_VISION_QUEUE_CAP,
                    encoded, settings.INTERVIEW_ANALYSIS_VISION_QUEUE_TTL_SECONDS, 'LPUSH'):
                raise VisionQueueFull('Vision queue capacity reached.')

    def _do_restore_message(self, payload, exchange, routing_key, pipe, leftmost=False):
        if routing_key != settings.INTERVIEW_ANALYSIS_VISION_QUEUE:
            return super()._do_restore_message(payload, exchange, routing_key, pipe, leftmost)
        encoded = dumps(payload)
        if len(encoded.encode('utf-8')) > 8192:
            return  # Recovery owns the dropped reference, just as for a full list.
        # Same Redis transaction as unacked removal; never bypass the queue cap.
        pipe.eval(BOUNDED_PUSH, 1, settings.INTERVIEW_ANALYSIS_VISION_QUEUE,
                  settings.INTERVIEW_ANALYSIS_VISION_QUEUE_CAP, encoded,
                  settings.INTERVIEW_ANALYSIS_VISION_QUEUE_TTL_SECONDS, 'LPUSH' if leftmost else 'RPUSH')


class Transport(RedisTransport):
    Channel = Channel
