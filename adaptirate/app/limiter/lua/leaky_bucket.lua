-- Leaky bucket as a meter (GCRA): smooths traffic to one request every window/limit.
-- burst + 1 requests may arrive together before spacing is enforced.
--
-- The theoretical arrival time is stored relative to a fixed recent epoch, in
-- microsecond precision. Stored as absolute epoch milliseconds through tostring()
-- (14 significant digits) it kept only 0.1 ms, which silently disabled the limit for
-- intervals shorter than that, i.e. above roughly 10,000 requests per second.
local EPOCH_MS = 1700000000000

local function run()
  local capacity = burst + 1
  if cost > capacity then return 0, 0, -1, 0 end
  local interval = window_ms / limit
  local now = now_ms - EPOCH_MS

  local tat = tonumber(redis.call('GET', KEYS[1])) or now
  if tat < now then tat = now end
  local new_tat = tat + cost * interval
  local allow_at = new_tat - capacity * interval

  if allow_at <= now then
    redis.call('SET', KEYS[1], string.format('%.6f', new_tat), 'PX', math.ceil(new_tat - now) + 1000)
    return 1, (capacity * interval - (new_tat - now)) / interval, 0, new_tat - now
  end
  local free = (capacity * interval - (tat - now)) / interval
  return 0, math.max(0, free), math.max(1, allow_at - now), tat - now
end
