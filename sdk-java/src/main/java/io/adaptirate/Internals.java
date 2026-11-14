package io.adaptirate;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.Iterator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.TreeSet;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.locks.ReentrantLock;
import java.util.function.BiPredicate;
import java.util.function.DoubleSupplier;

/** Transport-independent pieces shared by the client: leases, fallback, breaker, reporting. */
final class Internals {
    private Internals() {}

    /** Upper bounds (ms) of the latency histogram; must match the server. */
    static final double[] LATENCY_BOUNDS_MS = {1, 2, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000};

    static final DoubleSupplier MONOTONIC_SECONDS = () -> System.nanoTime() / 1e9;

    /**
     * Per-bucket maps (leases, fallback buckets, lease locks) are capped so a per-IP policy on
     * a public API cannot grow them forever.
     */
    static final int MAX_BUCKETS = 10_000;

    /**
     * Keeps an insertion-ordered map at or under {@link #MAX_BUCKETS}: idle entries go first,
     * then the oldest half.
     */
    static <V> void evictToBound(LinkedHashMap<String, V> entries, BiPredicate<String, V> isIdle) {
        if (entries.size() <= MAX_BUCKETS) {
            return;
        }
        entries.entrySet().removeIf(entry -> isIdle.test(entry.getKey(), entry.getValue()));
        if (entries.size() > MAX_BUCKETS) {
            int drop = entries.size() / 2;
            Iterator<String> oldest = entries.keySet().iterator();
            while (drop-- > 0 && oldest.hasNext()) {
                oldest.next();
                oldest.remove();
            }
        }
    }

    /**
     * One lock per bucket, created on demand and bounded. Only unlocked entries are dropped; a
     * thread that fetched a dropped lock but has not yet acquired it can race with a new one for
     * the same bucket, which costs at most one extra lease request.
     */
    static final class KeyedLocks {
        private final ConcurrentHashMap<String, ReentrantLock> locks = new ConcurrentHashMap<>();

        ReentrantLock get(String bucket) {
            ReentrantLock lock = locks.computeIfAbsent(bucket, ignored -> new ReentrantLock());
            if (locks.size() > MAX_BUCKETS) {
                locks.entrySet().removeIf(entry -> entry.getValue() != lock && !entry.getValue().isLocked());
            }
            return lock;
        }

        int size() {
            return locks.size();
        }
    }

    /**
     * Tokens leased from the server and spent locally. Expired tokens are discarded, never
     * carried over, so local decisions lag the server's limit by at most one lease.
     */
    static final class LeasePool {
        static final double MAX_LOCAL_BLOCK_SECONDS = 1.0;

        private static final class Lease {
            long tokens;
            double expiresAt;
            double blockedUntil;
            long limit;
            Decision last;
        }

        private final LinkedHashMap<String, Lease> leases = new LinkedHashMap<>();
        private final DoubleSupplier clock;

        LeasePool(DoubleSupplier clock) {
            this.clock = clock;
        }

        synchronized int size() {
            return leases.size();
        }

        /** A local decision, or null when the server must be asked. */
        synchronized Decision trySpend(String bucket, int cost, String policy) {
            Lease lease = leases.get(bucket);
            if (lease == null) {
                return null;
            }
            double now = clock.getAsDouble();
            if (now < lease.expiresAt && lease.tokens >= cost) {
                lease.tokens -= cost;
                return new Decision(true, lease.limit, lease.tokens, 0, 0, "ok", policy, "lease", 0);
            }
            if (now < lease.blockedUntil && lease.last != null) {
                long waitMs = Math.max(1, Math.round((lease.blockedUntil - now) * 1000));
                return new Decision(false, lease.last.limit(), 0, waitMs, lease.last.resetAfterMs(), lease.last.reason(), policy, "lease", 0);
            }
            return null;
        }

        /** Record a lease response and decide the request that triggered it. */
        synchronized Decision store(String bucket, long granted, int cost, long ttlMs, Decision decision) {
            double now = clock.getAsDouble();
            Decision result = apply(leases.computeIfAbsent(bucket, ignored -> new Lease()), granted, cost, ttlMs, decision, now);
            // Dropping a lease only costs an extra server round trip. Evicting after the update
            // keeps the lease just stored out of the idle set.
            evictToBound(leases, (ignored, lease) -> now >= lease.expiresAt && now >= lease.blockedUntil);
            return result;
        }

        private static Decision apply(Lease lease, long granted, int cost, long ttlMs, Decision decision, double now) {
            lease.limit = decision.limit();
            lease.last = decision;
            lease.expiresAt = now + ttlMs / 1000.0;
            if (granted >= cost) {
                lease.tokens = granted - cost;
                lease.blockedUntil = 0;
                return decision.with(true, lease.tokens, 0, "ok", "server");
            }
            // Not enough for this request. Keep what was granted for cheaper requests, and
            // stop asking the server until it could plausibly say yes.
            lease.tokens = granted;
            double wait = decision.retryAfterMs() > 0 ? decision.retryAfterMs() / 1000.0 : MAX_LOCAL_BLOCK_SECONDS;
            lease.blockedUntil = now + Math.min(wait, MAX_LOCAL_BLOCK_SECONDS);
            String reason = "ok".equals(decision.reason()) ? "limit" : decision.reason();
            return decision.with(false, granted, Math.max(1, decision.retryAfterMs()), reason, "server");
        }
    }

    /**
     * What to do when AdaptiRate cannot be reached. OPEN allows, CLOSED denies, and LOCAL
     * enforces the last limit the server reported with an in-process token bucket.
     */
    static final class LocalFallback {
        private record Known(long limit, double windowSeconds) {}

        private record Bucket(double tokens, double stamp) {}

        private final AdaptiveLimiter.FailMode mode;
        private final DoubleSupplier clock;
        private final Map<String, Known> known = new HashMap<>();
        private final LinkedHashMap<String, Bucket> buckets = new LinkedHashMap<>();

        LocalFallback(AdaptiveLimiter.FailMode mode, DoubleSupplier clock) {
            this.mode = mode;
            this.clock = clock;
        }

        synchronized int size() {
            return buckets.size();
        }

        synchronized void learn(Decision decision) {
            if (!"server".equals(decision.source()) || decision.limit() <= 0) {
                return;
            }
            if (decision.windowMs() > 0) {
                // The server states the window, so no guessing.
                known.put(decision.policy(), new Known(decision.limit(), decision.windowMs() / 1000.0));
                return;
            }
            double window = decision.resetAfterMs() > 0 ? decision.resetAfterMs() / 1000.0 : 0;
            Known previous = known.get(decision.policy());
            // Older servers: reset_after shrinks as a window elapses; the largest seen approximates
            // the window.
            double best = Math.max(window, previous == null ? 0 : previous.windowSeconds());
            known.put(decision.policy(), new Known(decision.limit(), best > 0 ? best : 1));
        }

        synchronized Decision decide(String policy, String bucket, int cost) {
            if (mode == AdaptiveLimiter.FailMode.CLOSED) {
                return new Decision(false, 0, 0, 1000, 1000, "fail_closed", policy, "fallback", 0);
            }
            Known limit = known.get(policy);
            if (mode == AdaptiveLimiter.FailMode.OPEN || limit == null) {
                return new Decision(true, 0, 0, 0, 0, "fail_open", policy, "fallback", 0);
            }
            double rate = limit.limit() / limit.windowSeconds();
            double now = clock.getAsDouble();
            Bucket state = buckets.getOrDefault(bucket, new Bucket(limit.limit(), now));
            double tokens = Math.min(limit.limit(), state.tokens() + (now - state.stamp()) * rate);
            if (tokens >= cost) {
                buckets.put(bucket, new Bucket(tokens - cost, now));
                evict(now);
                return new Decision(true, limit.limit(), (long) (tokens - cost), 0, 0, "fail_local", policy, "fallback", 0);
            }
            buckets.put(bucket, new Bucket(tokens, now));
            evict(now);
            long waitMs = (long) ((cost - tokens) / rate * 1000) + 1;
            return new Decision(false, limit.limit(), 0, waitMs, waitMs, "fail_local", policy, "fallback", 0);
        }

        /**
         * A dropped bucket restarts full, which errs toward availability: an outage never denies
         * more than it would have.
         */
        private void evict(double now) {
            evictToBound(buckets, (key, state) -> {
                Known limit = known.get(key.substring(0, Math.max(0, key.indexOf('\u0000'))));
                return limit == null || state.tokens() + (now - state.stamp()) * limit.limit() / limit.windowSeconds() >= limit.limit();
            });
        }
    }

    /** After a failure, short-circuit to the fallback briefly instead of timing out on every request. */
    static final class Breaker {
        private final double cooldownSeconds;
        private final DoubleSupplier clock;
        private volatile double openUntil;

        Breaker(double cooldownSeconds, DoubleSupplier clock) {
            this.cooldownSeconds = cooldownSeconds;
            this.clock = clock;
        }

        boolean isOpen() {
            return clock.getAsDouble() < openUntil;
        }

        void failure() {
            openUntil = clock.getAsDouble() + cooldownSeconds;
        }

        void success() {
            openUntil = 0;
        }
    }

    /** Aggregates what the protected service observes; drained once per report interval. */
    static final class Reporter {
        private static final class Window {
            long requests;
            long errors;
            long throttled;
            long timeouts;
            double latencySumMs;
            final long[] buckets = new long[LATENCY_BOUNDS_MS.length + 1];
        }

        private final String instanceId;
        private Map<String, Window> windows = new HashMap<>();
        private final Map<String, Integer> inFlight = new HashMap<>();
        private final Map<String, Integer> queueDepth = new HashMap<>();
        private final TreeSet<String> seen = new TreeSet<>();

        Reporter(String instanceId) {
            this.instanceId = instanceId;
        }

        synchronized void started(String policy) {
            inFlight.merge(policy, 1, Integer::sum);
            seen.add(policy);
        }

        synchronized void finished(String policy, double latencyMs, boolean error, boolean throttled, boolean timeout) {
            inFlight.merge(policy, -1, (current, delta) -> Math.max(0, current + delta));
            Window window = windows.computeIfAbsent(policy, ignored -> new Window());
            window.requests++;
            if (error) window.errors++;
            if (throttled) window.throttled++;
            if (timeout) window.timeouts++;
            window.latencySumMs += latencyMs;
            int index = LATENCY_BOUNDS_MS.length;
            for (int i = 0; i < LATENCY_BOUNDS_MS.length; i++) {
                if (latencyMs <= LATENCY_BOUNDS_MS[i]) {
                    index = i;
                    break;
                }
            }
            window.buckets[index]++;
        }

        synchronized void setQueueDepth(String policy, int depth) {
            queueDepth.put(policy, Math.max(0, depth));
            seen.add(policy);
        }

        /**
         * Reports to send. Policies with in-flight work report even when nothing finished, so
         * a stalled service still shows up as a growing in-flight count.
         */
        synchronized List<Map<String, Object>> drain() {
            Map<String, Window> drained = windows;
            windows = new HashMap<>();
            TreeSet<String> policies = new TreeSet<>(drained.keySet());
            for (String policy : seen) {
                if (inFlight.getOrDefault(policy, 0) > 0 || queueDepth.containsKey(policy)) {
                    policies.add(policy);
                }
            }
            List<Map<String, Object>> payloads = new ArrayList<>();
            for (String policy : policies) {
                Window window = drained.getOrDefault(policy, new Window());
                Map<String, Object> payload = new LinkedHashMap<>();
                payload.put("policy", policy);
                payload.put("instance_id", instanceId);
                payload.put("requests", window.requests);
                payload.put("errors", window.errors);
                payload.put("throttled", window.throttled);
                payload.put("timeouts", window.timeouts);
                payload.put("latency_sum_ms", Math.round(window.latencySumMs * 1000) / 1000.0);
                payload.put("latency_buckets", window.buckets);
                payload.put("in_flight", inFlight.getOrDefault(policy, 0));
                if (queueDepth.containsKey(policy)) {
                    payload.put("queue_depth", queueDepth.get(policy));
                }
                payloads.add(payload);
            }
            return payloads;
        }
    }
}
