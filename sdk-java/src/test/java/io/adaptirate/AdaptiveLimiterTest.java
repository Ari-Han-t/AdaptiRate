package io.adaptirate;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.HttpServer;
import io.adaptirate.Internals.LeasePool;
import io.adaptirate.Internals.LocalFallback;
import io.adaptirate.Internals.Reporter;
import java.io.IOException;
import java.net.InetSocketAddress;
import java.time.Duration;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.function.Function;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

/** SDK behaviour against a fake server on a local port. */
class AdaptiveLimiterTest {
    private static final ObjectMapper JSON = new ObjectMapper();

    record Call(String path, Map<String, Object> body, String apiKey) {}

    private HttpServer server;
    private final List<Call> calls = new CopyOnWriteArrayList<>();
    private final Map<String, Function<Map<String, Object>, Object>> responses = new LinkedHashMap<>();
    private volatile int status = 200;

    static Map<String, Object> wire(boolean allowed) {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("allowed", allowed);
        body.put("limit", 10);
        body.put("remaining", allowed ? 9 : 0);
        body.put("retry_after_ms", allowed ? 0 : 500);
        body.put("reset_after_ms", 60000);
        body.put("reason", allowed ? "ok" : "limit");
        body.put("policy", "api");
        return body;
    }

    static Map<String, Object> wire(boolean allowed, String field, Object value) {
        Map<String, Object> body = wire(allowed);
        body.put(field, value);
        return body;
    }

    @BeforeEach
    void start() throws IOException {
        server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/", exchange -> {
            Map<String, Object> body = JSON.readValue(exchange.getRequestBody(), new TypeReference<>() {});
            String path = exchange.getRequestURI().getPath();
            calls.add(new Call(path, body, exchange.getRequestHeaders().getFirst("X-API-Key")));
            Object answer = responses.getOrDefault(path, ignored -> wire(true)).apply(body);
            byte[] bytes = JSON.writeValueAsBytes(answer);
            exchange.getResponseHeaders().add("Content-Type", "application/json");
            exchange.sendResponseHeaders(status, bytes.length);
            exchange.getResponseBody().write(bytes);
            exchange.close();
        });
        server.start();
    }

    @AfterEach
    void stop() {
        server.stop(0);
    }

    private AdaptiveLimiter.Builder limiter() {
        return AdaptiveLimiter.builder()
                .apiKey("ar_test")
                .baseUrl("http://127.0.0.1:" + server.getAddress().getPort())
                .reportInterval(Duration.ZERO);
    }

    private List<String> paths() {
        return calls.stream().map(Call::path).toList();
    }

    @Test
    void requiresAnApiKey() {
        assertThatThrownBy(() -> AdaptiveLimiter.builder().apiKey(" ").build())
                .isInstanceOf(IllegalArgumentException.class)
                .hasMessageContaining("API key");
    }

    @Test
    void checkSendsSubjectAndKeyToEvaluate() {
        try (AdaptiveLimiter sdk = limiter().baseUrl("http://127.0.0.1:" + server.getAddress().getPort() + "/api/v1/").build()) {
            Decision decision = sdk.check("api", Subject.user("u1").withRole("premium").withCost(3));
            assertThat(decision.allowed()).isTrue();
            assertThat(decision.remaining()).isEqualTo(9);
            assertThat(decision.source()).isEqualTo("server");
        }
        Call call = calls.get(0);
        assertThat(call.path()).isEqualTo("/api/v1/evaluate");
        assertThat(call.body()).containsExactlyInAnyOrderEntriesOf(Map.of("policy", "api", "user", "u1", "role", "premium", "cost", 3));
        assertThat(call.apiKey()).isEqualTo("ar_test");
    }

    @Test
    void deniedDecisionExposesRetryHeadersAndAcquireThrows() {
        responses.put("/api/v1/evaluate", body -> {
            Map<String, Object> denied = wire(false, "retry_after_ms", 1500);
            denied.put("reset_after_ms", 2500);
            return denied;
        });
        try (AdaptiveLimiter sdk = limiter().build()) {
            Decision decision = sdk.check("api", Subject.user("u1"));
            assertThat(decision.allowed()).isFalse();
            assertThat(decision.headers()).containsExactly(
                    Map.entry("X-RateLimit-Limit", "10"),
                    Map.entry("X-RateLimit-Remaining", "0"),
                    Map.entry("X-RateLimit-Reset", "3"),
                    Map.entry("Retry-After", "2"));
            assertThatThrownBy(() -> sdk.acquire("api", Subject.user("u1")))
                    .isInstanceOfSatisfying(RateLimitExceededException.class,
                            error -> assertThat(error.retryAfterSeconds()).isEqualTo(1.5));
        }
    }

    @Test
    void configurationErrorsAreThrownNotSwallowed() {
        status = 404;
        responses.put("/api/v1/evaluate", body -> Map.of("detail", "Policy 'nope' does not exist"));
        try (AdaptiveLimiter sdk = limiter().build()) {
            assertThatThrownBy(() -> sdk.check("nope", Subject.user("u")))
                    .isInstanceOfSatisfying(AdaptiRateException.class, error -> {
                        assertThat(error.status()).isEqualTo(404);
                        assertThat(error.detail()).contains("does not exist");
                    });
        }
    }

    @Test
    void failModesWhenUnreachableOrErroring() {
        server.stop(0);
        try (AdaptiveLimiter open = limiter().failMode(AdaptiveLimiter.FailMode.OPEN).build();
                AdaptiveLimiter closed = limiter().failMode(AdaptiveLimiter.FailMode.CLOSED).build()) {
            Decision allowed = open.check("api", Subject.user("u"));
            assertThat(allowed.allowed()).isTrue();
            assertThat(allowed.reason()).isEqualTo("fail_open");
            assertThat(allowed.source()).isEqualTo("fallback");
            assertThat(closed.check("api", Subject.user("u")).reason()).isEqualTo("fail_closed");
        }
    }

    @Test
    void serverErrorsUseTheFallbackAndTheBreakerStopsHammering() {
        status = 503;
        try (AdaptiveLimiter sdk = limiter().failMode(AdaptiveLimiter.FailMode.CLOSED).cooldown(Duration.ofMinutes(1)).build()) {
            for (int i = 0; i < 20; i++) {
                assertThat(sdk.check("api", Subject.user("u")).allowed()).isFalse();
            }
        }
        assertThat(calls).hasSize(1);
    }

    @Test
    void localFailModeEnforcesTheLastKnownLimitPerSubject() {
        responses.put("/api/v1/evaluate", body -> wire(true, "limit", 5));
        try (AdaptiveLimiter sdk = limiter().failMode(AdaptiveLimiter.FailMode.LOCAL).cooldown(Duration.ofMinutes(1)).build()) {
            assertThat(sdk.check("api", Subject.user("u1")).allowed()).isTrue();
            server.stop(0);
            List<Boolean> outcomes = new ArrayList<>();
            for (int i = 0; i < 8; i++) {
                outcomes.add(sdk.check("api", Subject.user("u1")).allowed());
            }
            assertThat(outcomes).containsExactly(true, true, true, true, true, false, false, false);
            assertThat(sdk.check("api", Subject.user("u2")).allowed()).isTrue();
            assertThat(sdk.check("other", Subject.user("u1")).reason()).isEqualTo("fail_open");
        }
    }

    @Test
    void leasingSpendsLocallyAndRefillsWhenEmpty() {
        responses.put("/api/v1/lease", body -> {
            Map<String, Object> granted = wire(true, "granted", body.get("tokens"));
            granted.put("ttl_ms", 2000);
            return granted;
        });
        try (AdaptiveLimiter sdk = limiter().leaseSize(50).build()) {
            for (int i = 0; i < 120; i++) {
                assertThat(sdk.check("api", Subject.user("u1")).allowed()).isTrue();
            }
        }
        assertThat(paths()).containsExactly("/api/v1/lease", "/api/v1/lease", "/api/v1/lease");
        assertThat(calls.get(0).body()).containsEntry("tokens", 50).containsEntry("user", "u1");
    }

    @Test
    void aConcurrentBurstSharesOneLeaseRequest() throws Exception {
        responses.put("/api/v1/lease", body -> {
            Map<String, Object> granted = wire(true, "granted", body.get("tokens"));
            granted.put("ttl_ms", 2000);
            return granted;
        });
        ExecutorService pool = Executors.newFixedThreadPool(16);
        try (AdaptiveLimiter sdk = limiter().leaseSize(200).build()) {
            List<Future<Decision>> futures = new ArrayList<>();
            for (int i = 0; i < 200; i++) {
                futures.add(pool.submit(() -> sdk.check("api", Subject.user("u1"))));
            }
            for (Future<Decision> future : futures) {
                assertThat(future.get().allowed()).isTrue();
            }
        } finally {
            pool.shutdownNow();
        }
        assertThat(calls).hasSize(1);
    }

    @Test
    void partialAndEmptyLeases() {
        List<Integer> grants = new ArrayList<>(List.of(4, 0));
        responses.put("/api/v1/lease", body -> {
            Map<String, Object> denied = wire(false, "retry_after_ms", 800);
            denied.put("granted", grants.isEmpty() ? 0 : grants.remove(0));
            denied.put("ttl_ms", 2000);
            return denied;
        });
        List<Boolean> outcomes = new ArrayList<>();
        try (AdaptiveLimiter sdk = limiter().leaseSize(10).build()) {
            for (int i = 0; i < 8; i++) {
                outcomes.add(sdk.check("api", Subject.user("u1")).allowed());
            }
        }
        assertThat(outcomes).containsExactly(true, true, true, true, false, false, false, false);
        assertThat(calls).as("after an empty grant the SDK denies locally instead of asking each time").hasSize(2);
    }

    @Test
    void expiredLeaseTokensAreDiscarded() {
        double[] now = {0};
        LeasePool pool = new LeasePool(() -> now[0]);
        Decision decision = Decision.fromWire(wire(true), "server");
        assertThat(pool.store("b", 10, 1, 1000, decision).allowed()).isTrue();
        assertThat(pool.trySpend("b", 1, "api")).isNotNull();
        now[0] = 1.5;
        assertThat(pool.trySpend("b", 1, "api")).isNull();
    }

    @Test
    void localFallbackRefillsOverTime() {
        double[] now = {0};
        LocalFallback fallback = new LocalFallback(AdaptiveLimiter.FailMode.LOCAL, () -> now[0]);
        Map<String, Object> learned = wire(true, "limit", 2);
        learned.put("reset_after_ms", 1000);
        fallback.learn(Decision.fromWire(learned, "server"));
        assertThat(List.of(fallback.decide("api", "b", 1).allowed(), fallback.decide("api", "b", 1).allowed(),
                fallback.decide("api", "b", 1).allowed())).containsExactly(true, true, false);
        now[0] = 0.5;
        assertThat(fallback.decide("api", "b", 1).allowed()).isTrue();
        assertThat(fallback.decide("api", "b", 1).allowed()).isFalse();
    }

    @Test
    void trackReportsLatencyErrorsAndInFlight() throws Exception {
        try (AdaptiveLimiter sdk = limiter().build()) {
            assertThat(sdk.track("jobs", () -> "done")).isEqualTo("done");
            assertThatThrownBy(() -> sdk.track("jobs", () -> {
                throw new IllegalStateException("boom");
            })).isInstanceOf(IllegalStateException.class);
            sdk.flush();
        }
        Map<String, Object> report = calls.stream().filter(call -> call.path().equals("/api/v1/report")).findFirst().orElseThrow().body();
        assertThat(report).containsEntry("policy", "jobs").containsEntry("requests", 2).containsEntry("errors", 1).containsEntry("in_flight", 0);
        assertThat((List<?>) report.get("latency_buckets")).hasSize(Internals.LATENCY_BOUNDS_MS.length + 1);
    }

    @Test
    void onlyServerErrorsCountAndUpstreamThrottlesMustBeFlagged() {
        try (AdaptiveLimiter sdk = limiter().build()) {
            for (int code : new int[] {200, 503, 429, 404}) {
                sdk.started("api");
                sdk.finished("api", 10, code, false);
            }
            sdk.started("api");
            sdk.finished("api", 10, 502, true);
            sdk.flush();
        }
        Map<String, Object> report = calls.stream().filter(call -> call.path().equals("/api/v1/report")).findFirst().orElseThrow().body();
        assertThat(report).containsEntry("requests", 5).containsEntry("errors", 2).containsEntry("throttled", 1);
    }

    @Test
    void theReporterKeepsReportingStalledInFlightWork() {
        Reporter reporter = new Reporter("instance-1");
        reporter.started("api");
        reporter.started("api");
        reporter.finished("api", 3, false, false, false);
        Map<String, Object> first = reporter.drain().get(0);
        assertThat(first).containsEntry("in_flight", 1);
        assertThat(((long[]) first.get("latency_buckets"))[2]).isEqualTo(1);
        Map<String, Object> stalled = reporter.drain().get(0);
        assertThat(stalled).containsEntry("requests", 0L).containsEntry("in_flight", 1);
        reporter.finished("api", 99_999, true, false, false);
        reporter.drain();
        assertThat(reporter.drain()).isEmpty();
    }

    @Test
    void providerResponsesAreReportedWithHeaders() {
        try (AdaptiveLimiter sdk = limiter().build()) {
            sdk.reportProvider("openai", 429, Map.of("Retry-After", "7"));
        }
        assertThat(calls.get(0).path()).isEqualTo("/api/v1/providers/report");
        assertThat(calls.get(0).body()).containsEntry("provider", "openai").containsEntry("status", 429)
                .containsEntry("headers", Map.of("Retry-After", "7"));
    }

    @Test
    void awaitRetriesThenGivesUpPastMaxWait() throws Exception {
        List<Map<String, Object>> answers = new ArrayList<>(List.of(wire(false, "retry_after_ms", 20), wire(true)));
        responses.put("/api/v1/evaluate", body -> answers.size() > 1 ? answers.remove(0) : answers.get(0));
        try (AdaptiveLimiter sdk = limiter().build()) {
            assertThat(sdk.await("api", Subject.user("u"), Duration.ofSeconds(5)).allowed()).isTrue();
        }
        responses.put("/api/v1/evaluate", body -> wire(false, "retry_after_ms", 60_000));
        try (AdaptiveLimiter sdk = limiter().build()) {
            assertThatThrownBy(() -> sdk.await("api", Subject.user("u"), Duration.ofMillis(100)))
                    .isInstanceOf(RateLimitExceededException.class);
        }
    }
}
