/**
 * Typed fetch wrappers for the Governed NBA FastAPI backend.
 * Mirrors the Pydantic wire models in app/server.py.
 */

export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

export interface AppConfig {
  langsmith_org: string;
  langsmith_project: string;
  max_clarify_turns: number;
  high_risk_threshold: number;
  langsmith_links_enabled: boolean;
}

export interface ProfitMetrics {
  prob_acceptance: number;
  gross_expected_margin: number;
  expected_credit_loss: number;
  contact_cost: number;
  cannibalization_cost: number;
  expected_profit: number;
}

export interface Offer {
  offer_id: string;
  offer_name: string;
  product_category?: string;
  apr_range?: string;
  rewards_summary?: string;
  expected_profit?: number;
  profit_metrics?: ProfitMetrics;
  approved_compliance_template?: string;
}

export interface SuppressedOffer {
  offer_id: string;
  offer_name: string;
  reason: string;
}

export interface Customer {
  customer_id: number;
  full_name: string;
  risk_tier: string;
  annual_income: number;
  employment_status?: string;
  existing_debt?: number;
  customer_segment?: string;
  avg_checking_balance?: number;
  do_not_solicit?: number;
}

export interface TurnResponse {
  run_id: string | null;
  assistant_message: string;
  display_message: string;
  selected_offer?: Offer | null;
  candidate_offers?: Offer[] | null;
  suppressed_offers?: SuppressedOffer[] | null;
  high_risk_probability?: number | null;
  risk_blocked: boolean;
  governance_reason?: string | null;
  conversation_stage?: string | null;
  accumulated_features: Record<string, unknown>;
  customer_record?: Record<string, unknown> | null;
  clarify_count?: number | null;
}

export interface FeedbackResponse {
  ok: boolean;
  error?: string | null;
}

async function requestJson<T>(url: string, init?: RequestInit): Promise<T> {
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
    } catch {
      // ignore non-JSON error body
    }
    throw new ApiError(detail, res.status);
  }
  return (await res.json()) as T;
}

export async function fetchConfig(): Promise<AppConfig> {
  return requestJson<AppConfig>("/api/config");
}

export async function fetchCustomers(): Promise<Customer[]> {
  return requestJson<Customer[]>("/api/customers");
}

export async function postTurn(
  message: string,
  threadId: string,
  customerId?: number | null,
): Promise<TurnResponse> {
  const data = await requestJson<TurnResponse>("/api/turn", {
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

export async function postFeedback(
  runId: string,
  score: 0 | 1,
): Promise<FeedbackResponse> {
  return requestJson<FeedbackResponse>("/api/feedback", {
    method: "POST",
    body: JSON.stringify({ run_id: runId, score }),
  });
}
