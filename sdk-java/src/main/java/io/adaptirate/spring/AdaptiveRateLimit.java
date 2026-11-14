package io.adaptirate.spring;

import java.lang.annotation.Documented;
import java.lang.annotation.ElementType;
import java.lang.annotation.Retention;
import java.lang.annotation.RetentionPolicy;
import java.lang.annotation.Target;

/**
 * Rate-limit a controller method, or every method of a controller, against an AdaptiRate policy.
 *
 * <pre>{@code
 * @PostMapping("/orders")
 * @AdaptiveRateLimit("orders")
 * public Order create(@RequestBody NewOrder order) { ... }
 * }</pre>
 *
 * A method-level annotation overrides one on the class.
 */
@Target({ElementType.METHOD, ElementType.TYPE})
@Retention(RetentionPolicy.RUNTIME)
@Documented
public @interface AdaptiveRateLimit {
    /** The policy name. */
    String value();

    /** How many units one call consumes. */
    int cost() default 1;
}
