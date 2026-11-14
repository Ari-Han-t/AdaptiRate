package io.adaptirate.spring;

import io.adaptirate.AdaptiveLimiter;
import org.springframework.boot.autoconfigure.AutoConfiguration;
import org.springframework.boot.autoconfigure.condition.ConditionalOnClass;
import org.springframework.boot.autoconfigure.condition.ConditionalOnMissingBean;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.boot.context.properties.EnableConfigurationProperties;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.web.servlet.config.annotation.InterceptorRegistry;
import org.springframework.web.servlet.config.annotation.WebMvcConfigurer;

/**
 * Registers an {@link AdaptiveLimiter} and, in servlet web applications, the interceptor that
 * enforces {@link AdaptiveRateLimit}. Every bean backs off if the application defines its own.
 */
@AutoConfiguration
@EnableConfigurationProperties(AdaptiRateProperties.class)
public class AdaptiRateAutoConfiguration {

    @Bean(destroyMethod = "close")
    @ConditionalOnMissingBean
    public AdaptiveLimiter adaptiveLimiter(AdaptiRateProperties properties) {
        return AdaptiveLimiter.builder()
                .apiKey(properties.getApiKey())
                .baseUrl(properties.getUrl())
                .timeout(properties.getTimeout())
                .failMode(properties.getFailMode())
                .leaseSize(properties.getLeaseSize())
                .reportInterval(properties.getReportInterval())
                .build();
    }

    @Configuration(proxyBeanMethods = false)
    @ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
    @ConditionalOnClass(WebMvcConfigurer.class)
    static class WebMvc {

        @Bean
        @ConditionalOnMissingBean
        SubjectResolver adaptiRateSubjectResolver() {
            return SubjectResolver.principalAndAddress();
        }

        @Bean
        @ConditionalOnMissingBean
        AdaptiveRateLimitInterceptor adaptiveRateLimitInterceptor(AdaptiveLimiter limiter, SubjectResolver subjects) {
            return new AdaptiveRateLimitInterceptor(limiter, subjects);
        }

        @Bean
        WebMvcConfigurer adaptiRateWebMvcConfigurer(AdaptiveRateLimitInterceptor interceptor) {
            return new WebMvcConfigurer() {
                @Override
                public void addInterceptors(InterceptorRegistry registry) {
                    registry.addInterceptor(interceptor);
                }
            };
        }
    }
}
