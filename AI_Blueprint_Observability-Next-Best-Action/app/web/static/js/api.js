/**
 * Typed fetch wrappers for the Governed NBA FastAPI backend.
 * Mirrors the Pydantic wire models in app/server.py.
 */
export class ApiError extends Error {
    status;
    constructor(message, status) {
        super(message);
        this.name = "ApiError";
        this.status = status;
    }
}
async function requestJson(url, init) {
    const res = await fetch(url, {
        headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
        ...init,
    });
    if (!res.ok) {
        let detail = res.statusText || "Request failed";
        try {
            const errBody = await res.json();
            if (errBody && typeof errBody.detail === "string") {
                detail = errBody.detail;
            }
        }
        catch {
            // ignore non-JSON error body
        }
        throw new ApiError(detail, res.status);
    }
    return (await res.json());
}
export async function fetchConfig() {
    return requestJson("/api/config");
}
export async function fetchCustomers(params) {
    if (!params) {
        return requestJson("/api/customers");
    }
    const qs = new URLSearchParams();
    if (params.segment)
        qs.set("segment", params.segment);
    if (params.offer_id)
        qs.set("offer_id", params.offer_id);
    if (params.profitable_only)
        qs.set("profitable_only", "true");
    if (params.channel)
        qs.set("channel", params.channel);
    if (typeof params.limit === "number")
        qs.set("limit", String(params.limit));
    const suffix = qs.toString();
    return requestJson(suffix ? `/api/customers?${suffix}` : "/api/customers");
}
export async function fetchPortfolioAnalytics(channel = "BANKER_DESKTOP") {
    return requestJson(`/api/analytics/portfolio?channel=${encodeURIComponent(channel)}`);
}
export async function postTurn(message, threadId, customerId) {
    const data = await requestJson("/api/turn", {
        method: "POST",
        body: JSON.stringify({
            message,
            thread_id: threadId,
            customer_id: customerId ?? null,
        }),
    });
    return {
        ...data,
        run_id: data.run_id ?? null,
    };
}
export async function postPortfolioChatTurn(message, threadId, channel = "BANKER_DESKTOP") {
    const data = await requestJson("/api/analytics/chat", {
        method: "POST",
        body: JSON.stringify({
            message,
            thread_id: threadId,
            channel,
        }),
    });
    return {
        ...data,
        run_id: data.run_id ?? null,
        executed_sql: data.executed_sql ?? null,
    };
}
export async function postFeedback(runId, score) {
    return requestJson("/api/feedback", {
        method: "POST",
        body: JSON.stringify({ run_id: runId, score }),
    });
}
//# sourceMappingURL=api.js.map