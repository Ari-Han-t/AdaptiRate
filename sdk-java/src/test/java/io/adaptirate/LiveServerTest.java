package io.adaptirate;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.junit.jupiter.api.Assumptions.assumeTrue;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
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
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;

/**
 * SDK against a running AdaptiRate server. Skipped unless ADAPTIRATE_TEST_URL,
 * ADAPTIRATE_TEST_ADMIN_EMAIL and ADAPTIRATE_TEST_ADMIN_PASSWORD are set.
 */
class LiveServerTest {
    private static final ObjectMapper JSON = new ObjectMapper();
    private static final HttpClient HTTP = HttpClient.newBuilder().version(HttpClient.Version.HTTP_1_1).build();
    private static final String URL = System.getenv("ADAPTIRATE_TEST_URL");
    private static final List<String> created = new ArrayList<>();
    private static String token;
    private static String secret;
    private static String keyId;

    private static Map<String, Object> admin(String method, String path, Object body) throws Exception {
        HttpRequest.Builder request = HttpRequest.newBuilder(URI.create(URL + "/api/v1" + path))
                .header("Content-Type", "application/json")
                .method(method, body == null ? HttpRequest.BodyPublishers.noBody() : HttpRequest.BodyPublishers.ofString(JSON.writeValueAsString(body)));
        if (token != null) {
            request.header("Authorization", "Bearer " + token);
        }
        HttpResponse<String> response = HTTP.send(request.build(), HttpResponse.BodyHandlers.ofString());
        if (response.statusCode() >= 400) {
            throw new IllegalStateException(method + " " + path + " -> " + response.statusCode() + " " + response.body());
        }
        return response.body().isBlank() ? Map.of() : JSON.readValue(response.body(), new TypeReference<>() {});
    }

    @BeforeAll
    static void signIn() throws Exception {
        String email = System.getenv("ADAPTIRATE_TEST_ADMIN_EMAIL");
        String password = System.getenv("ADAPTIRATE_TEST_ADMIN_PASSWORD");
        assumeTrue(URL != null && email != null && password != null, "no live AdaptiRate server configured");
        token = (String) admin("POST", "/auth/login", Map.of("email", email, "password", password)).get("access_token");
        Map<String, Object> key = admin("POST", "/keys",
                Map.of("name", "java-sdk-tests-" + UUID.randomUUID().toString().substring(0, 6), "scopes", List.of("evaluate", "report")));
        secret = (String) key.get("secret");
        keyId = (String) key.get("id");
    }

    @AfterAll
    static void cleanUp() throws Exception {
        if (token == null) {
            return;
        }
        for (String id : created) {
            admin("DELETE", "/policies/" + id, null);
        }
        admin("POST", "/keys/" + keyId + "/revoke", null);
    }

    private static String policy(Map<String, Object> overrides) throws Exception {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("name", "java-" + UUID.randomUUID().toString().substring(0, 10));
        body.put("limit", 5);
        body.put("window_seconds", 60);
        body.put("scope", "user");
        body.put("algorithm", "fixed_window");
        body.put("adaptive", false);
        body.putAll(overrides);
        created.add((String) admin("POST", "/policies", body).get("id"));
        return (String) body.get("name");
    }

    private static AdaptiveLimiter.Builder limiter() {
        return AdaptiveLimiter.builder().apiKey(secret).baseUrl(URL).reportInterval(Duration.ZERO).timeout(Duration.ofSeconds(5));
    }

    @Test
    void enforcesARealLimitPerSubject() throws Exception {
        String name = policy(Map.of("limit", 3));
        try (AdaptiveLimiter sdk = limiter().build()) {
            List<Boolean> outcomes = new ArrayList<>();
            Decision last = null;
            for (int i = 0; i < 5; i++) {
                last = sdk.check(name, Subject.user("alice"));
                outcomes.add(last.allowed());
            }
            assertThat(outcomes).containsExactly(true, true, true, false, false);
            assertThat(last.reason()).isEqualTo("limit");
            assertThat(last.retryAfterMs()).isPositive();
            assertThat(sdk.check(name, Subject.user("bob")).allowed()).isTrue();
            assertThatThrownBy(() -> sdk.check("no-such-policy", Subject.user("alice")))
                    .isInstanceOfSatisfying(AdaptiRateException.class, error -> assertThat(error.status()).isEqualTo(404));
            assertThatThrownBy(() -> sdk.check(name, Subject.ip("10.0.0.1")))
                    .isInstanceOfSatisfying(AdaptiRateException.class, error -> assertThat(error.status()).isEqualTo(422));
        }
    }

    @Test
    void aBadApiKeyIsALoudErrorNotFailOpen() throws Exception {
        String name = policy(Map.of());
        try (AdaptiveLimiter sdk = limiter().apiKey("ar_wrong-key").build()) {
            assertThatThrownBy(() -> sdk.check(name, Subject.user("a")))
                    .isInstanceOfSatisfying(AdaptiRateException.class, error -> assertThat(error.status()).isEqualTo(401));
        }
    }

    @Test
    void concurrentChecksNeverOverAdmit() throws Exception {
        String name = policy(Map.of("limit", 20, "algorithm", "token_bucket"));
        ExecutorService pool = Executors.newFixedThreadPool(16);
        try (AdaptiveLimiter first = limiter().build(); AdaptiveLimiter second = limiter().build()) {
            List<Future<Decision>> futures = new ArrayList<>();
            for (int i = 0; i < 60; i++) {
                AdaptiveLimiter sdk = i % 2 == 0 ? first : second;
                futures.add(pool.submit(() -> sdk.check(name, Subject.user("shared"))));
            }
            int allowed = 0;
            for (Future<Decision> future : futures) {
                if (future.get().allowed()) allowed++;
            }
            assertThat(allowed).isEqualTo(20);
        } finally {
            pool.shutdownNow();
        }
    }

    @Test
    void leasingNeverAdmitsMoreThanTheLimit() throws Exception {
        String name = policy(Map.of("limit", 100, "algorithm", "token_bucket"));
        try (AdaptiveLimiter sdk = limiter().leaseSize(30).build()) {
            int allowed = 0;
            int local = 0;
            for (int i = 0; i < 150; i++) {
                Decision decision = sdk.check(name, Subject.user("worker"));
                if (decision.allowed()) allowed++;
                if ("lease".equals(decision.source())) local++;
            }
            assertThat(allowed).isEqualTo(100);
            assertThat(local).isGreaterThan(90);
        }
    }

    @Test
    void providerThrottlePausesTheBoundPolicy() throws Exception {
        String provider = "java-" + UUID.randomUUID().toString().substring(0, 8);
        String name = policy(Map.of("limit", 50, "provider", provider));
        try (AdaptiveLimiter sdk = limiter().build()) {
            sdk.track(name, () -> null);
            sdk.flush();
            assertThat(sdk.check(name, Subject.user("a")).allowed()).isTrue();
            sdk.reportProvider(provider, 429, Map.of("Retry-After", "30"));
            Decision paused = sdk.check(name, Subject.user("a"));
            assertThat(paused.allowed()).isFalse();
            assertThat(paused.reason()).isEqualTo("paused");
            assertThat(paused.retryAfterMs()).isBetween(25_000L, 30_000L);
        }
        admin("DELETE", "/providers/" + provider, null);
    }
}
