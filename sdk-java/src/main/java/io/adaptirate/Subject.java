package io.adaptirate;

import java.util.LinkedHashMap;
import java.util.Map;

/**
 * Who a request belongs to. Which attribute identifies a bucket is decided by the policy's
 * scope on the server; send every attribute you know and rules can match on any of them.
 */
public record Subject(String key, String user, String ip, String apiKey, String role, int cost) {

    public Subject {
        if (cost < 1) {
            throw new IllegalArgumentException("cost must be at least 1");
        }
    }

    public static Subject key(String key) {
        return new Subject(key, null, null, null, null, 1);
    }

    public static Subject user(String user) {
        return new Subject(null, user, null, null, null, 1);
    }

    public static Subject ip(String ip) {
        return new Subject(null, null, ip, null, null, 1);
    }

    public Subject withUser(String value) {
        return new Subject(key, value, ip, apiKey, role, cost);
    }

    public Subject withIp(String value) {
        return new Subject(key, user, value, apiKey, role, cost);
    }

    public Subject withApiKey(String value) {
        return new Subject(key, user, ip, value, role, cost);
    }

    public Subject withRole(String value) {
        return new Subject(key, user, ip, apiKey, value, cost);
    }

    public Subject withCost(int value) {
        return new Subject(key, user, ip, apiKey, role, value);
    }

    Map<String, Object> payload(String policy) {
        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("policy", policy);
        if (key != null) payload.put("key", key);
        if (user != null) payload.put("user", user);
        if (ip != null) payload.put("ip", ip);
        if (apiKey != null) payload.put("api_key", apiKey);
        if (role != null) payload.put("role", role);
        return payload;
    }

    /** Identity of the server-side bucket this subject maps to. */
    String bucket(String policy) {
        return String.join("\u0000", policy, nullToEmpty(key), nullToEmpty(user), nullToEmpty(ip), nullToEmpty(apiKey), nullToEmpty(role));
    }

    private static String nullToEmpty(String value) {
        return value == null ? "" : value;
    }
}
