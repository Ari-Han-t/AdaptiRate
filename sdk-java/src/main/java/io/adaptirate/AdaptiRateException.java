package io.adaptirate;

/** The server rejected the request itself: bad key, unknown policy, invalid input. */
public class AdaptiRateException extends RuntimeException {
    private final int status;
    private final String detail;

    public AdaptiRateException(int status, String detail) {
        super("AdaptiRate returned " + status + ": " + detail);
        this.status = status;
        this.detail = detail;
    }

    public int status() {
        return status;
    }

    public String detail() {
        return detail;
    }
}
