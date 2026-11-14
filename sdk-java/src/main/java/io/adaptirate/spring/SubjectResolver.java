package io.adaptirate.spring;

import io.adaptirate.Subject;
import jakarta.servlet.http.HttpServletRequest;
import java.security.Principal;

/**
 * Decides who a request belongs to. Declare your own bean to replace the default, for example
 * to read a tenant id or the caller's API key.
 */
@FunctionalInterface
public interface SubjectResolver {
    Subject resolve(HttpServletRequest request);

    /**
     * The authenticated principal's name when there is one, and the remote address.
     *
     * <p>Behind a reverse proxy, configure the container to rewrite the remote address from the
     * proxy headers it trusts (in Spring Boot, {@code server.forward-headers-strategy}).
     * X-Forwarded-For is not read here: without knowing which proxies are trusted, any caller
     * could choose its own identity.
     */
    static SubjectResolver principalAndAddress() {
        return request -> {
            Subject subject = Subject.ip(request.getRemoteAddr());
            Principal principal = request.getUserPrincipal();
            return principal == null ? subject : subject.withUser(principal.getName());
        };
    }
}
