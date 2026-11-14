package io.adaptirate.spring;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.content;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.header;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.HttpServer;
import io.adaptirate.AdaptiveLimiter;
import java.io.IOException;
import java.io.UncheckedIOException;
import java.net.InetSocketAddress;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CopyOnWriteArrayList;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.autoconfigure.EnableAutoConfiguration;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.context.annotation.Configuration;
import org.springframework.context.annotation.Import;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;

/** The starter wired into a real Spring MVC application, talking to a fake AdaptiRate server. */
@SpringBootTest(classes = SpringIntegrationTest.App.class)
@AutoConfigureMockMvc
class SpringIntegrationTest {
    private static final ObjectMapper JSON = new ObjectMapper();
    private static final List<Map<String, Object>> evaluations = new CopyOnWriteArrayList<>();
    private static final List<Map<String, Object>> reports = new CopyOnWriteArrayList<>();
    private static volatile boolean allow = true;
    private static final HttpServer server = startServer();

    private static HttpServer startServer() {
        try {
            HttpServer http = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
            http.createContext("/", exchange -> {
                Map<String, Object> body = JSON.readValue(exchange.getRequestBody(), new TypeReference<>() {});
                String path = exchange.getRequestURI().getPath();
                Map<String, Object> answer = new LinkedHashMap<>();
                if (path.endsWith("/evaluate")) {
                    evaluations.add(body);
                    answer.put("allowed", allow);
                    answer.put("limit", 10);
                    answer.put("remaining", allow ? 9 : 0);
                    answer.put("retry_after_ms", allow ? 0 : 2000);
                    answer.put("reset_after_ms", 60000);
                    answer.put("reason", allow ? "ok" : "limit");
                    answer.put("policy", body.get("policy"));
                } else {
                    reports.add(body);
                    answer.put("accepted", true);
                }
                byte[] bytes = JSON.writeValueAsBytes(answer);
                exchange.getResponseHeaders().add("Content-Type", "application/json");
                exchange.sendResponseHeaders(200, bytes.length);
                exchange.getResponseBody().write(bytes);
                exchange.close();
            });
            http.start();
            return http;
        } catch (IOException failure) {
            throw new UncheckedIOException(failure);
        }
    }

    @DynamicPropertySource
    static void properties(DynamicPropertyRegistry registry) {
        registry.add("adaptirate.url", () -> "http://127.0.0.1:" + server.getAddress().getPort());
        registry.add("adaptirate.api-key", () -> "ar_test");
        registry.add("adaptirate.report-interval", () -> "0s");
    }

    @AfterAll
    static void stop() {
        server.stop(0);
    }

    @Configuration
    @EnableAutoConfiguration
    @Import({OrdersController.class, ClassLevelController.class})
    static class App {}

    @RestController
    static class OrdersController {
        @GetMapping("/orders")
        @AdaptiveRateLimit(value = "orders", cost = 2)
        String orders() {
            return "orders";
        }

        @GetMapping("/broken")
        @AdaptiveRateLimit("orders")
        String broken() {
            throw new IllegalStateException("database down");
        }

        @GetMapping("/open")
        String open() {
            return "open";
        }
    }

    @RestController
    @AdaptiveRateLimit("catalog")
    static class ClassLevelController {
        @GetMapping("/catalog")
        String catalog() {
            return "catalog";
        }
    }

    @Autowired MockMvc mvc;
    @Autowired AdaptiveLimiter limiter;

    @BeforeEach
    void reset() {
        allow = true;
        limiter.flush();
        evaluations.clear();
        reports.clear();
    }

    @Test
    void annotatedHandlerIsCheckedAndGetsRateLimitHeaders() throws Exception {
        mvc.perform(get("/orders").with(request -> {
                    request.setRemoteAddr("203.0.113.9");
                    return request;
                }))
                .andExpect(status().isOk())
                .andExpect(content().string("orders"))
                .andExpect(header().string("X-RateLimit-Limit", "10"))
                .andExpect(header().string("X-RateLimit-Remaining", "9"));
        assertThat(evaluations).hasSize(1);
        assertThat(evaluations.get(0)).containsEntry("policy", "orders").containsEntry("ip", "203.0.113.9").containsEntry("cost", 2);
    }

    @Test
    void deniedRequestGets429AndNeverReachesTheHandler() throws Exception {
        allow = false;
        mvc.perform(get("/orders"))
                .andExpect(status().isTooManyRequests())
                .andExpect(header().string("Retry-After", "2"))
                .andExpect(jsonPath("$.reason").value("limit"))
                .andExpect(jsonPath("$.policy").value("orders"));
        limiter.flush();
        assertThat(reports).as("a blocked request is not reported as work done").isEmpty();
    }

    @Test
    void unannotatedHandlerIsUntouched() throws Exception {
        mvc.perform(get("/open")).andExpect(status().isOk()).andExpect(header().doesNotExist("X-RateLimit-Limit"));
        assertThat(evaluations).isEmpty();
    }

    @Test
    void classLevelAnnotationAppliesToItsMethods() throws Exception {
        mvc.perform(get("/catalog")).andExpect(status().isOk());
        assertThat(evaluations.get(0)).containsEntry("policy", "catalog").containsEntry("cost", 1);
    }

    @Test
    void latencyAndFailuresAreReported() throws Exception {
        mvc.perform(get("/orders")).andExpect(status().isOk());
        try {
            mvc.perform(get("/broken"));
        } catch (Exception expected) {
            // MockMvc rethrows unhandled controller exceptions; a real container answers 500.
        }
        limiter.flush();
        assertThat(reports).hasSize(1);
        assertThat(reports.get(0)).containsEntry("policy", "orders").containsEntry("requests", 2).containsEntry("errors", 1)
                .containsEntry("in_flight", 0);
    }
}
