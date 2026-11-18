/**
 * Zero-dependency SVG + HTML renderer for Portfolio Segmentation, Risk & Profitability Analytics.
 */

import type {
  FunnelStage,
  OfferAnalytics,
  PortfolioAnalyticsResponse,
  SegmentAnalytics,
} from "./api.js";

export interface AnalyticsDrilldownHandlers {
  onSelectSegment: (segment: SegmentAnalytics) => void;
  onSelectOffer: (offer: OfferAnalytics) => void;
}

function escapeHtml(raw: string): string {
  return raw
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function fmtInt(n: number): string {
  return Math.round(n).toLocaleString("en-US");
}

function fmtCurrency(val: number, compact = false): string {
  if (compact && Math.abs(val) >= 1_000_000) {
    return `$${(val / 1_000_000).toFixed(2)}M`;
  }
  return val.toLocaleString("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 0,
    maximumFractionDigits: 0,
  });
}

function renderKpis(container: HTMLElement, data: PortfolioAnalyticsResponse): void {
  const { kpis, channel, contact_cost } = data;
  const channelLabel =
    channel === "IN_APP"
      ? `In-App Self-Serve ($${contact_cost.toFixed(2)}/contact)`
      : `Branch Advisor ($${contact_cost.toFixed(2)}/contact)`;

  container.innerHTML = `
    <div class="kpi-card">
      <span class="kpi-card__label">Total Customer Book</span>
      <strong class="kpi-card__value">${fmtInt(kpis.total_customers)}</strong>
      <span class="kpi-card__sub">${data.segments.length} retail banking segments</span>
    </div>

    <div class="kpi-card">
      <span class="kpi-card__label">Suppressed by Governance</span>
      <strong class="kpi-card__value kpi-card__value--warn">${fmtInt(kpis.suppressed_customers)} (${kpis.suppressed_pct.toFixed(1)}%)</strong>
      <span class="kpi-card__sub">DNS registry, &lt;14d fatigue &amp; credit blocks</span>
    </div>

    <div class="kpi-card">
      <span class="kpi-card__label">Profitable Targetable Cohort</span>
      <strong class="kpi-card__value kpi-card__value--primary">${fmtInt(kpis.profitable_customers)} (${kpis.profitable_pct.toFixed(1)}%)</strong>
      <span class="kpi-card__sub">Cleared policy + Expected Profit &gt; $0</span>
    </div>

    <div class="kpi-card kpi-card--accent">
      <span class="kpi-card__label">Projected Annual Net Profit</span>
      <strong class="kpi-card__value kpi-card__value--good">${fmtCurrency(kpis.total_expected_profit, true)}</strong>
      <span class="kpi-card__sub">Avg ${fmtCurrency(kpis.avg_profit_per_targeted)}/targeted · ${escapeHtml(channelLabel)}</span>
    </div>
  `;
}

function renderFunnel(container: HTMLElement, stages: FunnelStage[]): void {
  if (stages.length === 0) {
    container.innerHTML = `<p class="hint">No funnel telemetry available.</p>`;
    return;
  }

  const rowsHtml = stages
    .map((stage, idx) => {
      const isFinal = stage.stage_id === "profitable_targetable" || idx === stages.length - 1;
      const widthScore = Math.max(0, Math.min(1000, Math.round(stage.pct_of_book * 10)));
      const fillVar = isFinal ? "var(--good)" : "var(--primary)";
      const dropBadge =
        stage.dropped_count > 0
          ? `<span class="funnel-drop" title="${escapeHtml(stage.drop_reason)}">−${fmtInt(stage.dropped_count)}</span>`
          : "";

      return `
        <div class="funnel-row ${isFinal ? "funnel-row--final" : ""}">
          <div class="funnel-row__label">
            ${escapeHtml(stage.label)}
            <span class="funnel-row__reason">${escapeHtml(stage.drop_reason)}</span>
          </div>
          <div class="funnel-track">
            <svg
              class="funnel-svg"
              viewBox="0 0 1000 22"
              preserveAspectRatio="none"
              role="img"
              aria-label="${escapeHtml(stage.label)}: ${fmtInt(stage.count)} customers (${stage.pct_of_book.toFixed(1)}%)"
            >
              <rect x="0" y="0" width="${widthScore}" height="22" rx="4" fill="${fillVar}" />
            </svg>
          </div>
          <div class="funnel-row__stats">
            ${dropBadge}
            <strong>${fmtInt(stage.count)}</strong>
            <span>(${stage.pct_of_book.toFixed(1)}%)</span>
          </div>
        </div>
      `;
    })
    .join("");

  container.innerHTML = `<div class="funnel-list">${rowsHtml}</div>`;
}

function renderSegmentRiskChart(
  container: HTMLElement,
  segments: SegmentAnalytics[],
  onSelectSegment: (seg: SegmentAnalytics) => void,
): void {
  const list = document.createElement("div");
  list.className = "cohort-list";

  for (const seg of segments) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "cohort-row";
    btn.title =
      seg.profitable_customers > 0
        ? `View profitable ${seg.label} customers in Advisor Workbench`
        : `Inspect ${seg.label} governance-suppressed profiles in Advisor Workbench`;

    // 100% stacked risk bar per segment so LOW (green), MED (amber), and HIGH (red) shares fill the bar cleanly
    const segTotal = Math.max(1, seg.total_customers);
    const wLow = Math.max(0, Math.round((seg.risk_counts.LOW / segTotal) * 1000));
    const wMed = Math.max(0, Math.round((seg.risk_counts.MED / segTotal) * 1000));
    const wHigh = Math.max(0, 1000 - wLow - wMed);
    const xMed = wLow;
    const xHigh = wLow + wMed;

    const pLow = Math.round((seg.risk_counts.LOW / segTotal) * 100);
    const pMed = Math.round((seg.risk_counts.MED / segTotal) * 100);
    const pHigh = Math.max(0, 100 - pLow - pMed);

    const profitBadge =
      seg.total_expected_profit > 0
        ? `<span class="profit-tag">${fmtCurrency(seg.total_expected_profit)} net/yr (avg ${fmtCurrency(seg.avg_expected_profit)})</span>`
        : `<span class="profit-tag--muted">$0 promotional (Servicing-only guardrail)</span>`;

    btn.innerHTML = `
      <div class="cohort-row__top">
        <span class="cohort-row__title">
          ${escapeHtml(seg.label)}
          <span class="cohort-row__cta">Filter cohort →</span>
        </span>
        <span class="cohort-row__kpi">
          <strong>${seg.profitable_pct.toFixed(1)}% profitable</strong>
          (${fmtInt(seg.profitable_customers)} / ${fmtInt(seg.total_customers)})
        </span>
      </div>
      <div class="cohort-bar-wrap">
        <svg
          class="cohort-svg"
          viewBox="0 0 1000 18"
          preserveAspectRatio="none"
          role="img"
          aria-label="${escapeHtml(seg.label)} risk split: ${seg.risk_counts.LOW} LOW (${pLow}%), ${seg.risk_counts.MED} MED (${pMed}%), ${seg.risk_counts.HIGH} HIGH (${pHigh}%)"
        >
          ${wLow > 0 ? `<rect x="0" y="0" width="${wLow}" height="18" fill="var(--good)" />` : ""}
          ${wMed > 0 ? `<rect x="${xMed}" y="0" width="${wMed}" height="18" fill="var(--warn)" />` : ""}
          ${wHigh > 0 ? `<rect x="${xHigh}" y="0" width="${wHigh}" height="18" fill="var(--bad)" />` : ""}
        </svg>
      </div>
      <div class="cohort-row__meta">
        <span>LOW: ${fmtInt(seg.risk_counts.LOW)} (${pLow}%) · MED: ${fmtInt(seg.risk_counts.MED)} (${pMed}%) · HIGH: ${fmtInt(seg.risk_counts.HIGH)} (${pHigh}%)</span>
        ${profitBadge}
      </div>
    `;

    btn.addEventListener("click", () => {
      onSelectSegment(seg);
    });
    list.appendChild(btn);
  }

  container.replaceChildren(list);
}

function renderOfferProfitChart(
  container: HTMLElement,
  offers: OfferAnalytics[],
  onSelectOffer: (offer: OfferAnalytics) => void,
): void {
  const maxProfit = Math.max(1, ...offers.map((o) => o.total_expected_profit));

  const list = document.createElement("div");
  list.className = "cohort-list";

  offers.forEach((offer, idx) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "cohort-row";
    const hasTargets = offer.primary_target_count > 0;
    if (!hasTargets) {
      btn.disabled = true;
      btn.style.cursor = "not-allowed";
      btn.style.opacity = "0.68";
    } else {
      btn.title = `Filter Advisor Workbench to customers whose #1 NBA is ${offer.offer_name}`;
    }

    const barWidth = Math.max(
      hasTargets ? 14 : 0,
      Math.round((offer.total_expected_profit / maxProfit) * 1000),
    );

    btn.innerHTML = `
      <div class="cohort-row__top">
        <span class="cohort-row__title">
          #${idx + 1} ${escapeHtml(offer.offer_name)}
          ${hasTargets ? `<span class="cohort-row__cta">Target ${fmtInt(offer.primary_target_count)} →</span>` : ""}
        </span>
        <span class="cohort-row__kpi">
          <strong>${fmtCurrency(offer.total_expected_profit)}</strong>/yr
        </span>
      </div>
      <div class="cohort-bar-wrap">
        <svg
          class="cohort-svg"
          viewBox="0 0 1000 18"
          preserveAspectRatio="none"
          role="img"
          aria-label="${escapeHtml(offer.offer_name)}: ${fmtCurrency(offer.total_expected_profit)} expected profit across ${fmtInt(offer.primary_target_count)} primary targets"
        >
          ${barWidth > 0 ? `<rect x="0" y="0" width="${barWidth}" height="18" fill="var(--primary)" />` : ""}
        </svg>
      </div>
      <div class="cohort-row__meta">
        <span>
          Primary #1 Cohort: <strong>${fmtInt(offer.primary_target_count)}</strong>
          (Eligible Book: ${fmtInt(offer.eligible_profitable_count)})
        </span>
        <span>
          ${
            hasTargets
              ? `Avg ${fmtCurrency(offer.avg_expected_profit)}/cust · ${offer.avg_propensity_pct.toFixed(0)}% est. acceptance`
              : `0 primary assignments (${escapeHtml(offer.target_risk_tier)} risk tier gate)`
          }
        </span>
      </div>
    `;

    if (hasTargets) {
      btn.addEventListener("click", () => {
        onSelectOffer(offer);
      });
    }
    list.appendChild(btn);
  });

  container.replaceChildren(list);
}

export function renderPortfolioDashboard(
  data: PortfolioAnalyticsResponse,
  handlers: AnalyticsDrilldownHandlers,
): void {
  const subtitleEl = document.getElementById("portfolio-subtitle");
  if (subtitleEl) {
    const channelDesc =
      data.channel === "IN_APP"
        ? `Customer In-App Self-Serve ($${data.contact_cost.toFixed(2)} contact cost)`
        : `Branch Advisor Assisted ($${data.contact_cost.toFixed(2)} contact cost)`;
    subtitleEl.textContent = `Evaluated across ${fmtInt(data.kpis.total_customers)} customer profiles · Active Channel Economics: ${channelDesc}`;
  }

  const kpisEl = document.getElementById("portfolio-kpis");
  const funnelEl = document.getElementById("chart-funnel");
  const segRiskEl = document.getElementById("chart-segment-risk");
  const offerProfitEl = document.getElementById("chart-offer-profit");

  if (kpisEl) renderKpis(kpisEl, data);
  if (funnelEl) renderFunnel(funnelEl, data.funnel);
  if (segRiskEl) renderSegmentRiskChart(segRiskEl, data.segments, handlers.onSelectSegment);
  if (offerProfitEl) renderOfferProfitChart(offerProfitEl, data.offers, handlers.onSelectOffer);
}
