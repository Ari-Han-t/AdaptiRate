package io.adaptirate;

import java.util.LinkedHashMap;
import java.util.Map;

/**
 * The answer to "may this request proceed?".
 *
 * @param source "server", "lease" (decided locally from leased tokens) or "fallback" (server unreachable)
 */
public record Decision(
        boolean allowed,
        long limit,
        long remaining,
        long retryAfterMs,
        long resetAfterMs,
        String reason,
        String policy,
        String source) {

    public double retryAfterSeconds() {
        return Math.max(0, retryAfterMs / 1000.0);
    }

    /** Standard rate-limit response headers for this decision. */
    public Map<String, String> headers() {
        Map<String, String> headers = new LinkedHashMap<>();
        headers.put("X-RateLimit-Limit", Long.toString(limit));
        headers.put("X-RateLimit-Remaining", Long.toString(Math.max(0, remaining)));
        headers.put("X-RateLimit-Reset", Long.toString(Math.max(0, (resetAfterMs + 999) / 1000)));
        if (!allowed && retryAfterMs > 0) {
            headers.put("Retry-After", Long.toString(Math.max(1, (retryAfterMs + 999) / 1000)));
        }
        return headers;
    }

    static Decision fromWire(Map<String, Object> body, String source) {
        return new Decision(
                Boolean.TRUE.equals(body.get("allowed")),
                number(body, "limit"),
                number(body, "remaining"),
                number(body, "retry_after_ms"),
                number(body, "reset_after_ms"),
                String.valueOf(body.get("reason")),
                String.valueOf(body.get("policy")),
                source);
    }

    static long number(Map<String, Object> body, String field) {
        Object value = body.get(field);
        return value instanceof Number number ? number.longValue() : 0L;
    }

    Decision with(boolean nowAllowed, long nowRemaining, long nowRetryAfterMs, String nowReason, String nowSource) {
        return new Decision(nowAllowed, limit, nowRemaining, nowRetryAfterMs, resetAfterMs, nowReason, policy, nowSource);
    }
}
