-- Shared epilogue: pause check, counters, reply.
-- reason: 0 allowed, 1 over limit, 2 paused (provider backoff), 3 cost can never fit
local allowed, remaining, retry_after, reset_after, reason = 0, 0, 0, 0, 0

local pause_ttl = redis.call('PTTL', KEYS[3])
if pause_ttl > 0 then
  reason = 2
  retry_after = pause_ttl
  reset_after = pause_ttl
else
  allowed, remaining, retry_after, reset_after = run()
  if allowed == 0 then
    if retry_after < 0 then reason = 3 else reason = 1 end
  end
end

if ARGV[9] == '1' and (allowed == 1 or ARGV[11] ~= '0') then
  local field = 'b'
  if allowed == 1 then field = 'a' end
  local policy_key = ARGV[5] .. now_s
  redis.call('HINCRBY', policy_key, field, cost)
  redis.call('EXPIRE', policy_key, 3700)
  local global_key = ARGV[6] .. now_s
  redis.call('HINCRBY', global_key, field, cost)
  redis.call('EXPIRE', global_key, 3700)
  local top_key = ARGV[7] .. field .. ':' .. math.floor(now_s / 3600)
  redis.call('ZINCRBY', top_key, cost, ARGV[8])
  redis.call('EXPIRE', top_key, 90000)
  -- Monotonic totals for Prometheus counters.
  redis.call('HINCRBY', ARGV[6] .. 'total', field .. ':' .. ARGV[10], cost)
end

if retry_after > 0 then retry_after = math.ceil(retry_after) end
return { allowed, limit, math.floor(remaining), retry_after, math.ceil(reset_after), reason }
