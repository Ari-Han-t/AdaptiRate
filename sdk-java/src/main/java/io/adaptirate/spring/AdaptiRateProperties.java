package io.adaptirate.spring;

import io.adaptirate.AdaptiveLimiter;
import java.time.Duration;
import org.springframework.boot.context.properties.ConfigurationProperties;

/**
 * Settings under {@code adaptirate.*}.
 *
 * <pre>
 * adaptirate.api-key=${ADAPTIRATE_API_KEY}
 * adaptirate.url=http://localhost:8080
 * adaptirate.fail-mode=local
 * </pre>
 */
@ConfigurationProperties(prefix = "adaptirate")
public class AdaptiRateProperties {
    /** API key with the evaluate and report permissions. Falls back to ADAPTIRATE_API_KEY. */
    private String apiKey;

    /** Base URL of the AdaptiRate server. Falls back to ADAPTIRATE_URL, then http://localhost:8080. */
    private String url;

    /** Per-request timeout. */
    private Duration timeout = Duration.ofSeconds(1);

    /** What to do when the server cannot be reached. */
    private AdaptiveLimiter.FailMode failMode = AdaptiveLimiter.FailMode.OPEN;

    /** Tokens to lease per round trip; 0 asks the server on every check. */
    private int leaseSize;

    /** How often latency and error reports are sent. */
    private Duration reportInterval = Duration.ofSeconds(1);

    public String getApiKey() {
        return apiKey;
    }

    public void setApiKey(String apiKey) {
        this.apiKey = apiKey;
    }

    public String getUrl() {
        return url;
    }

    public void setUrl(String url) {
        this.url = url;
    }

    public Duration getTimeout() {
        return timeout;
    }

    public void setTimeout(Duration timeout) {
        this.timeout = timeout;
    }

    public AdaptiveLimiter.FailMode getFailMode() {
        return failMode;
    }

    public void setFailMode(AdaptiveLimiter.FailMode failMode) {
        this.failMode = failMode;
    }

    public int getLeaseSize() {
        return leaseSize;
    }

    public void setLeaseSize(int leaseSize) {
        this.leaseSize = leaseSize;
    }

    public Duration getReportInterval() {
        return reportInterval;
    }

    public void setReportInterval(Duration reportInterval) {
        this.reportInterval = reportInterval;
    }
}
