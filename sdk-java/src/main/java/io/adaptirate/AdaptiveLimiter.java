package io.adaptirate;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import io.adaptirate.Internals.Breaker;
import io.adaptirate.Internals.LeasePool;
import io.adaptirate.Internals.LocalFallback;
import io.adaptirate.Internals.Reporter;
import java.io.IOException;
import java.net.InetAddress;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.Callable;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;
import java.util.concurrent.atomic.AtomicBoolean;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * Talks to an AdaptiRate server. Thread-safe; create one and share it.
 *
 * <pre>{@code
 * AdaptiveLimiter limiter = AdaptiveLimiter.builder().apiKey("ar_...").build();
 * if (limiter.check("api", Subject.user(userId)).allowed()) { ... }
 * }</pre>
 */
public final class AdaptiveLimiter implements AutoCloseable {
    private static final Logger log = LoggerFactory.getLogger(AdaptiveLimiter.class);
    private static final String API_PREFIX = "/api/v1";
    private static final TypeReference<Map<String, Object>> JSON_OBJECT = new TypeReference<>() {};

    /** What to do when the server cannot be reached. */
    public enum FailMode {
        /** Allow the request. */
        OPEN,
        /** Deny the request. */
        CLOSED,
        /** Enforce the last limit the server reported, in process. */
        LOCAL
    }

    private final String apiKey;
    private final String baseUrl;
    private final Duration timeout;
    private final int leaseSize;
    private final HttpClient http;
    private final ObjectMapper json = new ObjectMapper();
    private final LeasePool leases = new LeasePool(Internals.MONOTONIC_SECONDS);
    private final LocalFallback fallback;
    private final Breaker breaker;
    private final Reporter reporter;
    /** One lease request per bucket at a time, so a burst does not stampede the server. */
    private final ConcurrentHashMap<String, Object> leaseLocks = new ConcurrentHashMap<>();
    private final ScheduledExecutorService scheduler;
    private final AtomicBoolean closed = new AtomicBoolean();

    private AdaptiveLimiter(Builder builder) {
        String key = builder.apiKey != null ? builder.apiKey : System.getenv("ADAPTIRATE_API_KEY");
        if (key == null || key.isBlank()) {
            throw new IllegalArgumentException("An API key is required: set apiKey or ADAPTIRATE_API_KEY");
        }
        String url = builder.baseUrl != null ? builder.baseUrl : System.getenv("ADAPTIRATE_URL");
        if (url == null || url.isBlank()) {
            url = "http://localhost:8080";
        }
        url = url.replaceAll("/+$", "");
        if (url.endsWith(API_PREFIX)) {
            url = url.substring(0, url.length() - API_PREFIX.length());
        }
        if (builder.leaseSize < 0) {
            throw new IllegalArgumentException("leaseSize cannot be negative");
        }
        this.apiKey = key;
        this.baseUrl = url + API_PREFIX;
        this.timeout = builder.timeout;
        this.leaseSize = builder.leaseSize;
        // HTTP/1.1: over plain http:// the default HTTP/2 client attempts an h2c upgrade,
        // which common ASGI servers do not support and which loses the request body.
        this.http = HttpClient.newBuilder().version(HttpClient.Version.HTTP_1_1).connectTimeout(builder.timeout).build();
        this.fallback = new LocalFallback(builder.failMode, Internals.MONOTONIC_SECONDS);
        this.breaker = new Breaker(builder.cooldown.toMillis() / 1000.0, Internals.MONOTONIC_SECONDS);
        this.reporter = new Reporter(instanceId());

        if (builder.reportInterval.isZero() || builder.reportInterval.isNegative()) {
            this.scheduler = null;
        } else {
            this.scheduler = Executors.newSingleThreadScheduledExecutor(runnable -> {
                Thread thread = new Thread(runnable, "adaptirate-report");
                thread.setDaemon(true);
                return thread;
            });
            long millis = builder.reportInterval.toMillis();
            this.scheduler.scheduleWithFixedDelay(this::flushQuietly, millis, millis, TimeUnit.MILLISECONDS);
        }
    }

    public static Builder builder() {
        return new Builder();
    }

    private static String instanceId() {
        String host;
        try {
            host = InetAddress.getLocalHost().getHostName();
        } catch (IOException unknown) {
            host = "unknown";
        }
        return host + ":" + ProcessHandle.current().pid() + ":" + UUID.randomUUID().toString().substring(0, 6);
    }

    // ---- decisions ----------------------------------------------------------

    /** The JSON body, or null when the server could not be reached or failed. */
    private Map<String, Object> post(String path, Object payload) {
        if (breaker.isOpen()) {
            return null;
        }
        HttpResponse<String> response;
        try {
            HttpRequest request = HttpRequest.newBuilder(URI.create(baseUrl + path))
                    .timeout(timeout)
                    .header("Content-Type", "application/json")
                    .header("X-API-Key", apiKey)
                    .header("User-Agent", "adaptirate-java/0.1.0")
                    .POST(HttpRequest.BodyPublishers.ofString(json.writeValueAsString(payload)))
                    .build();
            response = http.send(request, HttpResponse.BodyHandlers.ofString());
        } catch (InterruptedException interrupted) {
            Thread.currentThread().interrupt();
            breaker.failure();
            return null;
        } catch (IOException unreachable) {
            breaker.failure();
            log.warn("AdaptiRate unreachable ({}); using the configured fail mode", unreachable.toString());
            return null;
        }
        if (response.statusCode() >= 500) {
            breaker.failure();
            log.warn("AdaptiRate returned {}; using the configured fail mode", response.statusCode());
            return null;
        }
        breaker.success();
        Map<String, Object> body;
        try {
            body = json.readValue(response.body(), JSON_OBJECT);
        } catch (IOException malformed) {
            throw new AdaptiRateException(response.statusCode(), "unreadable response body");
        }
        if (response.statusCode() >= 400) {
            Object detail = body.get("detail");
            throw new AdaptiRateException(response.statusCode(), detail instanceof String text ? text : String.valueOf(detail));
        }
        return body;
    }

    /** Ask whether one request may proceed. Never throws for a denied request. */
    public Decision check(String policy, Subject subject) {
        String bucket = subject.bucket(policy);
        int cost = subject.cost();
        Map<String, Object> payload = subject.payload(policy);

        if (leaseSize > 0) {
            Decision local = leases.trySpend(bucket, cost, policy);
            if (local != null) {
                return local;
            }
            synchronized (leaseLocks.computeIfAbsent(bucket, ignored -> new Object())) {
                // Another thread may have refilled the lease while this one waited.
                local = leases.trySpend(bucket, cost, policy);
                if (local != null) {
                    return local;
                }
                payload.put("tokens", Math.max(leaseSize, cost));
                Map<String, Object> body = post("/lease", payload);
                if (body == null) {
                    return fallback.decide(policy, bucket, cost);
                }
                Decision decision = Decision.fromWire(body, "server");
                fallback.learn(decision);
                return leases.store(bucket, Decision.number(body, "granted"), cost, Decision.number(body, "ttl_ms"), decision);
            }
        }

        payload.put("cost", cost);
        Map<String, Object> body = post("/evaluate", payload);
        if (body == null) {
            return fallback.decide(policy, bucket, cost);
        }
        Decision decision = Decision.fromWire(body, "server");
        fallback.learn(decision);
        return decision;
    }

    /** Like {@link #check}, but throws {@link RateLimitExceededException} when denied. */
    public Decision acquire(String policy, Subject subject) {
        Decision decision = check(policy, subject);
        if (!decision.allowed()) {
            throw new RateLimitExceededException(decision);
        }
        return decision;
    }

    /** Block until allowed, sleeping for the server's retry-after between attempts. */
    public Decision await(String policy, Subject subject, Duration maxWait) throws InterruptedException {
        long deadline = System.nanoTime() + maxWait.toNanos();
        while (true) {
            Decision decision = check(policy, subject);
            if (decision.allowed()) {
                return decision;
            }
            long waitNanos = TimeUnit.MILLISECONDS.toNanos(Math.max(1, decision.retryAfterMs()));
            if (decision.retryAfterMs() < 0 || System.nanoTime() + waitNanos > deadline) {
                throw new RateLimitExceededException(decision);
            }
            TimeUnit.NANOSECONDS.sleep(waitNanos);
        }
    }

    // ---- signals ------------------------------------------------------------

    /** Time a unit of work and report it, so the adaptive engine can see how the service is doing. */
    public <T> T track(String policy, Callable<T> work) throws Exception {
        started(policy);
        long start = System.nanoTime();
        boolean error = false;
        boolean timedOut = false;
        try {
            return work.call();
        } catch (TimeoutException | java.net.http.HttpTimeoutException timeoutFailure) {
            timedOut = true;
            throw timeoutFailure;
        } catch (Exception failure) {
            error = true;
            throw failure;
        } finally {
            reporter.finished(policy, (System.nanoTime() - start) / 1e6, error, false, timedOut);
        }
    }

    /** Mark a request as in flight. Pair with {@link #finished}. */
    public void started(String policy) {
        reporter.started(policy);
    }

    /**
     * Complete a request marked with {@link #started}.
     *
     * @param status the response status this service produced. Only 5xx counts as an error: a
     *     429 the service chose to send is load shedding, not ill health.
     * @param upstreamThrottled whether the work failed because an upstream rejected it with 429
     */
    public void finished(String policy, double latencyMs, int status, boolean upstreamThrottled) {
        reporter.finished(policy, latencyMs, status >= 500, upstreamThrottled, false);
    }

    public void setQueueDepth(String policy, int depth) {
        reporter.setQueueDepth(policy, depth);
    }

    /** Tell AdaptiRate what an upstream provider answered, so it can pace against that provider's quota. */
    public void reportProvider(String provider, int status, Map<String, String> headers) {
        Map<String, String> trimmed = new LinkedHashMap<>();
        for (Map.Entry<String, String> header : headers.entrySet()) {
            if (trimmed.size() >= 100) break;
            String name = header.getKey();
            String value = header.getValue();
            trimmed.put(name.length() > 128 ? name.substring(0, 128) : name, value.length() > 1024 ? value.substring(0, 1024) : value);
        }
        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("provider", provider);
        payload.put("status", status);
        payload.put("headers", trimmed);
        try {
            post("/providers/report", payload);
        } catch (AdaptiRateException rejected) {
            log.warn("provider report rejected: {}", rejected.getMessage());
        }
    }

    /** Convenience for {@code java.net.http} responses. */
    public void reportProvider(String provider, HttpResponse<?> response) {
        Map<String, String> headers = new LinkedHashMap<>();
        response.headers().map().forEach((name, values) -> headers.put(name, String.join(", ", values)));
        reportProvider(provider, response.statusCode(), headers);
    }

    /** Send pending reports now. */
    public void flush() {
        List<Map<String, Object>> payloads = new ArrayList<>(reporter.drain());
        for (Map<String, Object> payload : payloads) {
            try {
                post("/report", payload);
            } catch (AdaptiRateException rejected) {
                log.warn("report for {} rejected: {}", payload.get("policy"), rejected.getMessage());
            }
        }
    }

    private void flushQuietly() {
        try {
            flush();
        } catch (RuntimeException unexpected) {
            log.warn("failed to send reports", unexpected);
        }
    }

    @Override
    public void close() {
        if (!closed.compareAndSet(false, true)) {
            return;
        }
        if (scheduler != null) {
            scheduler.shutdownNow();
        }
        flushQuietly();
    }

    public static final class Builder {
        private String apiKey;
        private String baseUrl;
        private Duration timeout = Duration.ofSeconds(1);
        private FailMode failMode = FailMode.OPEN;
        private int leaseSize;
        private Duration reportInterval = Duration.ofSeconds(1);
        private Duration cooldown = Duration.ofSeconds(1);

        private Builder() {}

        /** Defaults to the ADAPTIRATE_API_KEY environment variable. */
        public Builder apiKey(String value) {
            this.apiKey = value;
            return this;
        }

        /** Defaults to ADAPTIRATE_URL, then http://localhost:8080. */
        public Builder baseUrl(String value) {
            this.baseUrl = value;
            return this;
        }

        public Builder timeout(Duration value) {
            this.timeout = value;
            return this;
        }

        public Builder failMode(FailMode value) {
            this.failMode = value;
            return this;
        }

        /** Tokens to lease per round trip; 0 asks the server on every check. */
        public Builder leaseSize(int value) {
            this.leaseSize = value;
            return this;
        }

        /** How often to send latency and error reports. Zero disables the background thread. */
        public Builder reportInterval(Duration value) {
            this.reportInterval = value;
            return this;
        }

        /** After a failed call, skip the server for this long. */
        public Builder cooldown(Duration value) {
            this.cooldown = value;
            return this;
        }

        public AdaptiveLimiter build() {
            return new AdaptiveLimiter(this);
        }
    }
}
