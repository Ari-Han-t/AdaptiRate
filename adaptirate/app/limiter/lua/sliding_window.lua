-- Sliding window counter: current window plus the previous one weighted by overlap.
-- Constant memory per key, no edge bursts.
local function run()
  if cost > limit then return 0, 0, -1, 0 end
  local idx = math.floor(now_ms / window_ms)

  local state = redis.call('HMGET', KEYS[1], 'i', 'c', 'p')
  local stored_idx = tonumber(state[1])
  local current = tonumber(state[2]) or 0
  local previous = tonumber(state[3]) or 0
  if stored_idx == nil or stored_idx < idx - 1 then
    current = 0
    previous = 0
  elseif stored_idx == idx - 1 then
    previous = current
    current = 0
  end

  local elapsed = now_ms - idx * window_ms
  local weight = 1 - elapsed / window_ms
  local used = previous * weight + current

  local ok = 0
  if used + cost <= limit then
    current = current + cost
    used = used + cost
    ok = 1
  end
  redis.call('HSET', KEYS[1], 'i', idx, 'c', current, 'p', previous)
  redis.call('PEXPIRE', KEYS[1], window_ms * 2 + 1000)

  local retry = 0
  if ok == 0 then
    local room = limit - current - cost
    if room >= 0 and previous > 0 then
      -- wait until the previous window's weight decays enough
      retry = (1 - room / previous) * window_ms - elapsed
    else
      -- current window alone is too full: wait for it to become "previous" and decay
      retry = window_ms - elapsed
      if current > 0 then
        retry = retry + (1 - math.max(0, limit - cost) / current) * window_ms
      end
    end
    retry = math.max(1, retry)
  end
  return ok, math.max(0, limit - used), retry, window_ms - elapsed
end
