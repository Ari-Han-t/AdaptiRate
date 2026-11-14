# Spring Boot

Install the starter from this repository (it is not on Maven Central):

```bash
cd /path/to/AdaptiRate/sdk-java && mvn install -DskipTests
```

**pom.xml**

```xml
<dependency>
  <groupId>io.adaptirate</groupId>
  <artifactId>adaptirate-spring-boot-starter</artifactId>
  <version>0.1.0</version>
</dependency>
```

**application.properties**

```properties
adaptirate.api-key=${ADAPTIRATE_API_KEY}
adaptirate.url=http://localhost:8080
adaptirate.fail-mode=local
# Behind a reverse proxy, so request.getRemoteAddr() is the real client:
server.forward-headers-strategy=native
```

**A controller**

```java
@RestController
@RequestMapping("/orders")
@AdaptiveRateLimit("orders")                       // every method in this controller
class OrdersController {

    @PostMapping
    Order create(@RequestBody NewOrder order) { ... }

    @PostMapping("/bulk")
    @AdaptiveRateLimit(value = "orders", cost = 10) // overrides the class-level one
    List<Order> bulk(@RequestBody List<NewOrder> orders) { ... }
}
```

That is the whole integration. The starter registers an `AdaptiveLimiter` bean and an MVC
interceptor that checks the limit, answers `429` with `Retry-After` when it is exceeded, and
reports each handler's latency and status to the adaptive engine.

By default the caller is identified by the authenticated principal's name and the remote
address. To use something else, declare a bean:

```java
@Bean
SubjectResolver subjectResolver() {
    return request -> Subject.key(request.getHeader("X-Tenant")).withIp(request.getRemoteAddr());
}
```

Outside MVC handlers (scheduled jobs, outbound calls), inject the limiter directly:

```java
@Service
class Mailer {
    private final AdaptiveLimiter limiter;
    Mailer(AdaptiveLimiter limiter) { this.limiter = limiter; }

    void send(String userId, Email email) throws Exception {
        limiter.acquire("emails", Subject.user(userId));
        limiter.track("emails", () -> smtp.send(email));
    }
}
```
