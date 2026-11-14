package io.adaptirate.spring;

import io.adaptirate.AdaptiveLimiter;
import io.adaptirate.Decision;
import jakarta.servlet.DispatcherType;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import java.io.IOException;
import org.springframework.core.annotation.AnnotatedElementUtils;
import org.springframework.web.method.HandlerMethod;
import org.springframework.web.servlet.HandlerInterceptor;

/**
 * Enforces {@link AdaptiveRateLimit} on Spring MVC handlers and reports each handler's latency
 * and outcome to the adaptive engine.
 */
public class AdaptiveRateLimitInterceptor implements HandlerInterceptor {
    private static final String START_ATTRIBUTE = AdaptiveRateLimitInterceptor.class.getName() + ".start";
    private static final String POLICY_ATTRIBUTE = AdaptiveRateLimitInterceptor.class.getName() + ".policy";

    private final AdaptiveLimiter limiter;
    private final SubjectResolver subjects;

    public AdaptiveRateLimitInterceptor(AdaptiveLimiter limiter, SubjectResolver subjects) {
        this.limiter = limiter;
        this.subjects = subjects;
    }

    private static AdaptiveRateLimit annotationFor(Object handler) {
        if (!(handler instanceof HandlerMethod method)) {
            return null;
        }
        AdaptiveRateLimit onMethod = AnnotatedElementUtils.findMergedAnnotation(method.getMethod(), AdaptiveRateLimit.class);
        return onMethod != null ? onMethod : AnnotatedElementUtils.findMergedAnnotation(method.getBeanType(), AdaptiveRateLimit.class);
    }

    @Override
    public boolean preHandle(HttpServletRequest request, HttpServletResponse response, Object handler) throws IOException {
        // Spring calls preHandle again on the async dispatch of a Callable/DeferredResult handler.
        // Charging again would double-count the request and leak an in-flight slot, because
        // afterCompletion runs once.
        if (request.getDispatcherType() != DispatcherType.REQUEST) {
            return true;
        }
        AdaptiveRateLimit limit = annotationFor(handler);
        if (limit == null) {
            return true;
        }
        Decision decision = limiter.check(limit.value(), subjects.resolve(request).withCost(limit.cost()));
        decision.headers().forEach(response::setHeader);
        if (!decision.allowed()) {
            response.setStatus(429);
            response.setContentType("application/json");
            response.getWriter().write("{\"detail\":\"Rate limit exceeded\",\"policy\":\"" + escape(decision.policy())
                    + "\",\"reason\":\"" + escape(decision.reason()) + "\",\"retry_after_ms\":" + decision.retryAfterMs() + "}");
            return false;
        }
        limiter.started(limit.value());
        request.setAttribute(START_ATTRIBUTE, System.nanoTime());
        request.setAttribute(POLICY_ATTRIBUTE, limit.value());
        return true;
    }

    @Override
    public void afterCompletion(HttpServletRequest request, HttpServletResponse response, Object handler, Exception failure) {
        // Only set when preHandle admitted the request, so blocked requests are not reported as work.
        if (!(request.getAttribute(START_ATTRIBUTE) instanceof Long start)) {
            return;
        }
        String policy = (String) request.getAttribute(POLICY_ATTRIBUTE);
        request.removeAttribute(START_ATTRIBUTE);
        int status = failure != null && response.getStatus() < 500 ? 500 : response.getStatus();
        limiter.finished(policy, (System.nanoTime() - start) / 1e6, status, false);
    }

    private static String escape(String text) {
        StringBuilder escaped = new StringBuilder(text.length());
        for (char character : text.toCharArray()) {
            if (character == '"' || character == '\\') {
                escaped.append('\\').append(character);
            } else if (character < 0x20) {
                escaped.append(String.format("\\u%04x", (int) character));
            } else {
                escaped.append(character);
            }
        }
        return escaped.toString();
    }
}
