-- Adaptive hybrid: a small token bucket shapes bursts, a sliding window counter
-- enforces the hard ceiling. A request must pass both; state commits atomically.
local function run()
  local capacity = burst
  if capacity <= 0 then capacity = math.max(1, math.ceil(limit / 5)) end
  if capacity > limit then capacity = limit end
  if cost > capacity then return 0, 0, -1, 0 end
  local rate = limit / window_ms
  local idx = math.floor(now_ms / window_ms)

  local state = redis.call('HMGET', KEYS[1], 't', 'ts', 'i', 'c', 'p')
  local tokens = tonumber(state[1])
  local ts = tonumber(state[2])
  if tokens == nil or ts == nil then
    tokens = capacity
    ts = now_ms
  end
  tokens = math.min(capacity, tokens + math.max(0, now_ms - ts) * rate)

  local stored_idx = tonumber(state[3])
  local current = tonumber(state[4]) or 0
  local previous = tonumber(state[5]) or 0
  if stored_idx == nil or stored_idx < idx - 1 then
    current = 0
    previous = 0
  elseif stored_idx == idx - 1 then
    previous = current
    current = 0
  end
  local elapsed = now_ms - idx * window_ms
  local used = previous * (1 - elapsed / window_ms) + current

  local bucket_ok = tokens >= cost
  local window_ok = used + cost <= limit
  local ok = 0
  if bucket_ok and window_ok then
    tokens = tokens - cost
    current = current + cost
    used = used + cost
    ok = 1
  end
  redis.call('HSET', KEYS[1], 't', tostring(tokens), 'ts', tostring(now_ms), 'i', idx, 'c', current, 'p', previous)
  redis.call('PEXPIRE', KEYS[1], window_ms * 2 + 1000)

  local retry = 0
  if ok == 0 then
    if not bucket_ok then retry = (cost - tokens) / rate end
    if not window_ok then
      local room = limit - current - cost
      local wait = window_ms - elapsed
      if room >= 0 and previous > 0 then
        wait = (1 - room / previous) * window_ms - elapsed
      elseif current > 0 then
        wait = wait + (1 - math.max(0, limit - cost) / current) * window_ms
      end
      retry = math.max(retry, wait)
    end
    retry = math.max(1, retry)
  end
  return ok, math.max(0, math.min(tokens, limit - used)), retry, window_ms - elapsed
end
