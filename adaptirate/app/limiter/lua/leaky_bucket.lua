-- Leaky bucket as a meter (GCRA): smooths traffic to one request every window/limit.
-- burst + 1 requests may arrive together before spacing is enforced.
local function run()
  local capacity = burst + 1
  if cost > capacity then return 0, 0, -1, 0 end
  local interval = window_ms / limit

  local tat = tonumber(redis.call('GET', KEYS[1])) or now_ms
  if tat < now_ms then tat = now_ms end
  local new_tat = tat + cost * interval
  local allow_at = new_tat - capacity * interval

  if allow_at <= now_ms then
    redis.call('SET', KEYS[1], tostring(new_tat), 'PX', math.ceil(new_tat - now_ms) + 1000)
    return 1, (capacity * interval - (new_tat - now_ms)) / interval, 0, new_tat - now_ms
  end
  local free = (capacity * interval - (tat - now_ms)) / interval
  return 0, math.max(0, free), math.max(1, allow_at - now_ms), tat - now_ms
end
