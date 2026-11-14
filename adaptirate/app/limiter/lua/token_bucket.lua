-- Token bucket: refills at limit/window, holds up to limit + burst tokens.
local function run()
  local capacity = limit + burst
  if cost > capacity then return 0, 0, -1, 0 end
  local rate = limit / window_ms

  local state = redis.call('HMGET', KEYS[1], 't', 'ts')
  local tokens = tonumber(state[1])
  local ts = tonumber(state[2])
  if tokens == nil or ts == nil then
    tokens = capacity
    ts = now_ms
  end
  tokens = math.min(capacity, tokens + math.max(0, now_ms - ts) * rate)

  local ok = 0
  if tokens >= cost then
    tokens = tokens - cost
    ok = 1
  end
  redis.call('HSET', KEYS[1], 't', tostring(tokens), 'ts', tostring(now_ms))
  redis.call('PEXPIRE', KEYS[1], math.ceil(capacity / rate) + 1000)

  local retry = 0
  if ok == 0 then retry = math.max(1, (cost - tokens) / rate) end
  return ok, tokens, retry, (capacity - tokens) / rate
end
