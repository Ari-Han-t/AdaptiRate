-- Fixed window: one counter per aligned window. Cheapest; allows 2x bursts at window edges.
local function run()
  if cost > limit then return 0, 0, -1, 0 end
  local idx = math.floor(now_ms / window_ms)
  local key = KEYS[1] .. ':' .. idx
  local current = tonumber(redis.call('GET', key)) or 0
  local reset = (idx + 1) * window_ms - now_ms

  if current + cost <= limit then
    redis.call('INCRBY', key, cost)
    redis.call('PEXPIRE', key, window_ms + 1000)
    return 1, limit - current - cost, 0, reset
  end
  return 0, math.max(0, limit - current), math.max(1, reset), reset
end
