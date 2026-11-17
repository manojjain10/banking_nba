/**
 * Governed NBA Observability & Decisioning Panel.
 *
 * Surfaces:
 *  1. High-risk probability hero & guardrail status (with exact governance reason).
 *  2. Customer Segment, Channel, and Clarify turn telemetry.
 *  3. Winning Offer with Expected Profit Objective Function breakdown.
 *  4. Policy-First Suppression Audit Log (why ineligible/unprofitable offers were blocked).
 */

import type { Offer, TurnResponse } from "./api.js";
import { langsmithThreadUrl, state } from "./state.js";

const STAGE_LABELS: Record<string, string> = {
  gathering: "Gathering information",
  offer_presented: "Offer presented",
  post_offer: "Post-offer chat",
};

type Status = "good" | "warn" | "critical" | "neutral";

function section(title: string): HTMLElement {
  const el = document.createElement("h3");
  el.className = "obs__section";
  el.textContent = title;
  return el;
}

function hint(text: string): HTMLElement {
  const el = document.createElement("p");
  el.className = "obs__hint";
  el.textContent = text;
  return el;
}

function tile(
  label: string,
  value: string,
  status: Status = "neutral",
  wide = false,
): HTMLElement {
  const el = document.createElement("div");
  el.className = wide ? "tile tile--wide" : "tile";

  const labelEl = document.createElement("div");
  labelEl.className = "tile__label";
  labelEl.textContent = label;
  el.appendChild(labelEl);

  const valueEl = document.createElement("div");
  valueEl.className = "tile__value";
  if (status !== "neutral") {
    const dot = document.createElement("span");
    dot.className = `dot-mark dot-mark--${status}`;
    valueEl.appendChild(dot);
  }
  valueEl.append(document.createTextNode(value));
  el.appendChild(valueEl);

  return el;
}

function riskHero(result: TurnResponse): HTMLElement {
  const box = document.createElement("div");
  box.className = "obs__hero";

  const label = document.createElement("div");
  label.className = "obs__hero-label";
  label.textContent = "Governance & credit risk score";
  box.appendChild(label);

  const probability = result.high_risk_probability;
  const value = document.createElement("div");
  value.className = "obs__hero-value";

  if (probability === null || probability === undefined) {
    value.classList.add("obs__hero-value--muted");
    value.textContent = "n/a";
    box.appendChild(value);
    box.appendChild(hint("Send a message to evaluate governance & risk."));
    return box;
  }

  const pct = probability * 100;
  value.textContent = `${pct.toFixed(2)}%`;
  box.appendChild(value);

  const threshold = state.config?.high_risk_threshold ?? 0.5;
  const breached = probability >= threshold || result.risk_blocked;

  const meter = document.createElement("div");
  meter.className = "meter";
  meter.setAttribute("role", "img");
  meter.setAttribute(
    "aria-label",
    `Risk score ${pct.toFixed(2)} percent, guardrail threshold ${(
      threshold * 100
    ).toFixed(0)} percent`,
  );

  const fill = document.createElement("div");
  fill.className = breached ? "meter__fill meter__fill--breach" : "meter__fill";
  fill.style.width = `${Math.min(100, Math.max(0, pct))}%`;
  meter.appendChild(fill);

  const tick = document.createElement("div");
  tick.className = "meter__threshold";
  tick.style.left = `${Math.min(100, Math.max(0, threshold * 100))}%`;
  meter.appendChild(tick);

  box.appendChild(meter);

  const scale = document.createElement("div");
  scale.className = "meter__scale";
  const zero = document.createElement("span");
  zero.textContent = "0%";
  const mark = document.createElement("span");
  mark.textContent = `threshold ${(threshold * 100).toFixed(0)}%`;
  const hundred = document.createElement("span");
  hundred.textContent = "100%";
  scale.append(zero, mark, hundred);
  box.appendChild(scale);

  if (result.governance_reason) {
    box.appendChild(hint(result.governance_reason));
  }

  return box;
}

function clarifyTile(result: TurnResponse): HTMLElement {
  const max = state.config?.max_clarify_turns ?? 3;
  const count = result.clarify_count ?? 0;
  const capped = count >= max;

  const el = tile(
    "Clarify turns",
    capped ? `${count} / ${max} — cap reached` : `${count} / ${max}`,
    capped ? "warn" : "neutral",
    true,
  );

  const segments = document.createElement("div");
  segments.className = "segments";
  for (let i = 0; i < max; i += 1) {
    const seg = document.createElement("div");
    const filled = i < count;
    seg.className = filled
      ? capped
        ? "segment segment--cap"
        : "segment segment--on"
      : "segment";
    segments.appendChild(seg);
  }
  el.appendChild(segments);
  return el;
}

function offerCard(offer: Offer): HTMLElement {
  const box = document.createElement("div");
  box.className = "obs__offer";

  const name = document.createElement("div");
  name.className = "obs__offer-name";
  name.textContent = offer.offer_name ?? offer.offer_id;
  box.appendChild(name);

  const id = document.createElement("code");
  id.className = "obs__offer-id";
  const catTag = offer.product_category ? ` · ${offer.product_category}` : "";
  id.textContent = `${offer.offer_id}${catTag}`;
  box.appendChild(id);

  if (offer.profit_metrics) {
    const pm = offer.profit_metrics;
    const profitLine = document.createElement("div");
    profitLine.className = "obs__offer-apr";
    profitLine.textContent = `Expected Profit: $${pm.expected_profit.toFixed(2)} (Accept ${(
      pm.prob_acceptance * 100
    ).toFixed(0)}%)`;
    box.appendChild(profitLine);

    const breakdown = document.createElement("div");
    breakdown.className = "obs__offer-meta";
    breakdown.textContent = `Margin $${pm.gross_expected_margin.toFixed(
      2,
    )} − Loss $${pm.expected_credit_loss.toFixed(
      2,
    )} − Contact $${pm.contact_cost.toFixed(
      2,
    )} − Cannib. $${pm.cannibalization_cost.toFixed(2)}`;
    box.appendChild(breakdown);
  }

  if (typeof offer.apr_range === "string" && offer.apr_range) {
    const apr = document.createElement("div");
    apr.className = "obs__offer-meta";
    apr.textContent = `Terms: ${offer.apr_range}`;
    box.appendChild(apr);
  }

  if (typeof offer.rewards_summary === "string" && offer.rewards_summary) {
    const rewards = document.createElement("div");
    rewards.className = "obs__offer-meta";
    rewards.textContent = offer.rewards_summary;
    box.appendChild(rewards);
  }

  return box;
}

function collapsible(title: string, payload: unknown): HTMLElement {
  const details = document.createElement("details");
  details.className = "obs__details";

  const summary = document.createElement("summary");
  summary.textContent = title;
  details.appendChild(summary);

  const pre = document.createElement("pre");
  pre.className = "obs__json";
  pre.textContent = JSON.stringify(payload, null, 2);
  details.appendChild(pre);

  return details;
}

export function renderObservability(container: HTMLElement): void {
  container.replaceChildren();

  const result = state.lastResult;

  // --- Governance & Risk Hero ---------------------------------------
  container.appendChild(section("Governance & customer risk"));
  container.appendChild(
    riskHero(
      result ?? ({ high_risk_probability: null } as unknown as TurnResponse),
    ),
  );

  // --- Turn & Segmentation KPIs -------------------------------------
  container.appendChild(section("Decision telemetry"));

  if (!result) {
    container.appendChild(hint("Send a message to populate graph telemetry."));
  } else {
    const tiles = document.createElement("div");
    tiles.className = "obs__tiles";

    const stage = result.conversation_stage;
    tiles.appendChild(
      tile("Stage", stage ? (STAGE_LABELS[stage] ?? stage) : "n/a"),
    );
    tiles.appendChild(
      result.risk_blocked
        ? tile("Policy gate", "Suppressed", "critical")
        : tile("Policy gate", "Cleared", "good"),
    );

    const custRec = result.customer_record ?? {};
    const segment =
      typeof custRec.customer_segment === "string"
        ? custRec.customer_segment
        : "General";
    const channel =
      typeof custRec.preferred_channel === "string"
        ? custRec.preferred_channel
        : "IN_APP";

    tiles.appendChild(tile("Segment", segment, "neutral"));
    tiles.appendChild(tile("Channel", channel, "neutral"));
    tiles.appendChild(clarifyTile(result));
    container.appendChild(tiles);

    if (result.selected_offer) {
      container.appendChild(section("Winning NBA (Ranked #1 Profit)"));
      container.appendChild(offerCard(result.selected_offer));
    }

    container.appendChild(section("Governance & graph audit"));
    container.appendChild(
      collapsible(
        `Suppressed offers (${(result.suppressed_offers ?? []).length})`,
        result.suppressed_offers ?? [],
      ),
    );
    container.appendChild(
      collapsible(
        "Profit-ranked candidates",
        (result.candidate_offers ?? []).map((offer) => ({
          offer_id: offer.offer_id,
          offer_name: offer.offer_name,
          expected_profit: offer.expected_profit,
          profit_metrics: offer.profit_metrics,
        })),
      ),
    );
    container.appendChild(
      collapsible("Customer profile & features", {
        customer_segment: custRec.customer_segment,
        avg_checking_balance: custRec.avg_checking_balance,
        credit_score: custRec.credit_score,
        current_products: custRec.current_products,
        do_not_solicit: custRec.do_not_solicit,
        extracted_features: result.accumulated_features,
      }),
    );
  }

  // --- Session ------------------------------------------------------
  container.appendChild(section("Session"));

  const thread = document.createElement("div");
  thread.className = "obs__thread";
  thread.textContent = state.threadId;
  thread.title = state.threadId;
  container.appendChild(thread);

  const url = langsmithThreadUrl();
  if (url) {
    const link = document.createElement("a");
    link.className = "obs__link";
    link.href = url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = "Open thread in LangSmith ↗";
    container.appendChild(link);
  } else {
    container.appendChild(
      hint("Set LANGSMITH_ORG to enable one-click thread links."),
    );
  }
}
