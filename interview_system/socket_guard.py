"""Redis-only ephemeral limits. Never raw frames, credentials, or durable jobs."""
from django.conf import settings
from redis.asyncio import Redis

# All participating processes use one Redis primary and the same prefix. TIME
# supplies the rate/lease clock; scripts atomically check every applicable key.
RESERVE = """
local t=redis.call('TIME'); local now=t[1]*1000+t[2]/1000
local expiry=math.min(now+tonumber(ARGV[2]),tonumber(ARGV[3]))
if expiry<=now then return 0 end
for i,key in ipairs(KEYS) do
 redis.call('ZREMRANGEBYSCORE',key,'-inf',now)
 if ARGV[4]=='refresh' then
  if not redis.call('ZSCORE',key,ARGV[1]) then return 0 end
 elseif redis.call('ZCARD',key)>=tonumber(ARGV[4+i]) then return 0 end
end
for _,key in ipairs(KEYS) do
 redis.call('ZADD',key,expiry,ARGV[1])
 local ttl=math.max(redis.call('PTTL',key),math.ceil(expiry-now)+1000)
 redis.call('PEXPIRE',key,ttl)
end
return 1
"""
RELEASE = """
for _,key in ipairs(KEYS) do
 redis.call('ZREM',key,ARGV[1])
 if redis.call('ZCARD',key)==0 then redis.call('DEL',key) end
end
return 1
"""
RATE = """
local t=redis.call('TIME'); local now=t[1]*1000+t[2]/1000
local remaining={}
for i,key in ipairs(KEYS) do
 local rate=tonumber(ARGV[i*2-1]); local burst=tonumber(ARGV[i*2])
 local old=redis.call('HMGET',key,'tokens','at')
 local tokens=tonumber(old[1]) or burst; local at=tonumber(old[2]) or now
 tokens=math.min(burst,tokens+math.max(0,now-at)*rate/1000)
 if tokens<0.999999 then return 0 end
 remaining[i]=tokens-1
end
for i,key in ipairs(KEYS) do
 redis.call('HSET',key,'tokens',remaining[i],'at',now)
 redis.call('PEXPIRE',key,10000)
end
return 1
"""


class SocketGuard:
    def __init__(self, lease, purpose, connection_id):
        self.lease = lease
        self.purpose = purpose
        self.connection_id = connection_id
        self.prefix = settings.INTERVIEW_ANALYSIS_REDIS_PREFIX
        self.redis = Redis.from_url(settings.INTERVIEW_ANALYSIS_REDIS_URL, socket_connect_timeout=1,
                                    socket_timeout=1, max_connections=2)
        self.keys = [f'{self.prefix}:connections:user:{lease.user_id}',
                     f'{self.prefix}:connections:session:{lease.session_id}:{purpose}',
                     f'{self.prefix}:connections:global']

    async def reserve(self, refresh=False):
        limits = [max(1, settings.INTERVIEW_ANALYSIS_MAX_USER_SOCKETS),
                  1 if self.purpose == 'publisher' else max(1, settings.INTERVIEW_ANALYSIS_MAX_SESSION_OBSERVERS),
                  max(1, settings.INTERVIEW_ANALYSIS_MAX_SOCKETS)]
        return bool(await self.redis.eval(RESERVE, len(self.keys), *self.keys, self.connection_id,
            max(1, settings.INTERVIEW_ANALYSIS_GUARD_LEASE_SECONDS)*1000,
            int(self.lease.expires_at.timestamp()*1000), 'refresh' if refresh else 'reserve', *limits))

    async def allow(self, kind):
        keys = [f'{self.prefix}:rate:{kind}:user:{self.lease.user_id}',
                f'{self.prefix}:rate:{kind}:session:{self.lease.session_id}']
        if kind == 'frame':
            args = [max(1, settings.INTERVIEW_ANALYSIS_USER_FRAME_FPS), 1,
                    min(3, max(1, settings.INTERVIEW_ANALYSIS_FRAME_FPS)), 1]
        else:
            user_rate = max(1, settings.INTERVIEW_ANALYSIS_USER_MESSAGE_RATE)
            session_rate = max(1, settings.INTERVIEW_ANALYSIS_SESSION_MESSAGE_RATE)
            args = [user_rate, user_rate, session_rate, session_rate]
        return bool(await self.redis.eval(RATE, len(keys), *keys, *args))

    async def release(self):
        await self.redis.eval(RELEASE, len(self.keys), *self.keys, self.connection_id)

    async def aclose(self):
        await self.redis.aclose()
