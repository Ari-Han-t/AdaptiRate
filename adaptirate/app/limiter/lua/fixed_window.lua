-- Fixed window: one counter per aligned window. Cheapest; allows 2x bursts at window edges.
--
-- The window index lives inside the hash rather than in the key name. A key such as
-- "<subject>:<index>" would let a caller whose subject is literally "victim:<index>"
-- write into the victim's counter.
local function run()
  if cost > limit then return 0, 0, -1, 0 end
  local idx = math.floor(now_ms / window_ms)
  local reset = (idx + 1) * window_ms - now_ms

  local state = redis.call('HMGET', KEYS[1], 'i', 'c')
  local current = 0
  if tonumber(state[1]) == idx then current = tonumber(state[2]) or 0 end

  if current + cost <= limit then
    redis.call('HSET', KEYS[1], 'i', idx, 'c', current + cost)
    redis.call('PEXPIRE', KEYS[1], window_ms + 1000)
    return 1, limit - current - cost, 0, reset
  end
  return 0, math.max(0, limit - current), math.max(1, reset), reset
end
