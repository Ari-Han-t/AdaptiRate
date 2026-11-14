package io.adaptirate;

/** Thrown by {@link AdaptiveLimiter#acquire} when a request is denied. */
public class RateLimitExceededException extends RuntimeException {
    private final transient Decision decision;

    public RateLimitExceededException(Decision decision) {
        super("Rate limit exceeded for policy '" + decision.policy() + "' (" + decision.reason() + ")");
        this.decision = decision;
    }

    public Decision decision() {
        return decision;
    }

    public double retryAfterSeconds() {
        return decision.retryAfterSeconds();
    }
}
