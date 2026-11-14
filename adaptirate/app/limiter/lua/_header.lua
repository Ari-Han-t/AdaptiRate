-- Shared preamble for every algorithm script.
--
-- KEYS[1] limiter state   KEYS[2] adaptive multiplier   KEYS[3] pause flag
-- ARGV[1] base limit      ARGV[2] window (ms)           ARGV[3] burst
-- ARGV[4] cost            ARGV[5] policy counter prefix ARGV[6] global counter prefix
-- ARGV[7] top-N prefix    ARGV[8] top-N member          ARGV[9] "1" to record counters
-- ARGV[10] policy name     ARGV[11] "0" to skip recording a refusal (lease probes)
--
-- Time comes from Redis so every caller shares one clock.
local base_limit = tonumber(ARGV[1])
local window_ms = tonumber(ARGV[2])
local burst = tonumber(ARGV[3])
local cost = tonumber(ARGV[4])

local redis_time = redis.call('TIME')
local now_s = tonumber(redis_time[1])
local now_ms = now_s * 1000 + math.floor(tonumber(redis_time[2]) / 1000)

local mult = tonumber(redis.call('GET', KEYS[2])) or 1
local limit = math.max(1, math.floor(base_limit * mult + 0.5))
