/**
 * Entry point: Branch Advisor Workbench + Customer 360 Explorer + LangGraph Chat.
 */
import { renderPortfolioDashboard } from "./analytics.js";
import { ApiError, fetchConfig, fetchCustomers, fetchPortfolioAnalytics, postTurn, } from "./api.js";
import { renderChat } from "./chat.js";
import { resetConversation, state } from "./state.js";
import { currentTheme, syncToggleButton, toggleTheme } from "./theme.js";
import { showToast } from "./toast.js";
const wb = {
    dossier: null,
    selectedOfferIdx: 0,
    simAmount: 15000,
    defaultCustomers: [],
    filteredCustomers: [],
    role: "BANKER_DESKTOP",
    activeView: "WORKBENCH",
    portfolioData: null,
    cohortFilterLabel: null,
};
function byId(id) {
    const el = document.getElementById(id);
    if (!el)
        throw new Error(`missing #${id} in index.html`);
    return el;
}
function money(val, decimals = 0) {
    return val.toLocaleString("en-US", {
        style: "currency",
        currency: "USD",
        minimumFractionDigits: decimals,
        maximumFractionDigits: decimals,
    });
}
function populateCustomerSelect(list) {
    const select = byId("customer-select");
    select.replaceChildren();
    if (list.length === 0) {
        const opt = document.createElement("option");
        opt.textContent = "No matching customers";
        select.appendChild(opt);
        select.disabled = true;
        return;
    }
    select.disabled = false;
    for (const c of list) {
        const opt = document.createElement("option");
        opt.value = String(c.customer_id);
        const seg = c.customer_segment ? ` · ${c.customer_segment}` : "";
        opt.textContent = `${c.full_name} (ID: #${c.customer_id})${seg}`;
        select.appendChild(opt);
    }
    if (list.some((c) => c.customer_id === state.customerId)) {
        select.value = String(state.customerId);
    }
    else if (list[0]) {
        state.customerId = list[0].customer_id;
        select.value = String(state.customerId);
    }
}
function channelExpectedProfit(offer) {
    const pm = offer?.profit_metrics ?? {};
    const baseExpected = Number(offer?.expected_profit ?? pm.expected_profit ?? 0);
    if (typeof pm.contact_cost === "number") {
        const targetContactCost = wb.role === "IN_APP" ? 0.5 : 18.0;
        return Math.round((baseExpected + (pm.contact_cost - targetContactCost)) * 100) / 100;
    }
    return baseExpected;
}
async function loadCustomerDossier(customerId) {
    const hint = byId("customer-hint");
    hint.textContent = "Loading profile…";
    hint.classList.remove("hint--warn");
    try {
        const res = await fetch(`/api/customers/${customerId}/research?channel=${encodeURIComponent(wb.role)}`);
        if (!res.ok)
            throw new Error(`HTTP ${res.status}`);
        wb.dossier = await res.json();
        wb.selectedOfferIdx = 0;
        const cust = wb.dossier.customer_record ?? {};
        const bal = Number(cust.avg_checking_balance ?? 15000);
        wb.simAmount = bal >= 15000 ? Math.round(bal * 0.65) : 5000;
        byId("sim-amount").value = String(wb.simAmount);
        // Clear previous outcome radio selection
        document
            .querySelectorAll('input[name="outcome"]')
            .forEach((r) => (r.checked = false));
        byId("outcome-feedback").textContent = "";
        hint.textContent = wb.dossier.risk_blocked
            ? "⚠ Suppressed by Governance Gate"
            : `${cust.risk_tier ?? "LOW"} Risk · Eligible`;
        if (wb.dossier.risk_blocked)
            hint.classList.add("hint--warn");
        renderAllPanels();
    }
    catch (err) {
        hint.textContent = "Could not load dossier";
        hint.classList.add("hint--warn");
        showToast(`Failed to load customer #${customerId}: ${err instanceof Error ? err.message : String(err)}`, "error");
    }
}
function renderAllPanels() {
    if (!wb.dossier)
        return;
    renderColumn1Snapshot();
    renderColumn2Offers();
    renderColumn3ActionPanel();
}
/** COLUMN 1: CUSTOMER SNAPSHOT */
function renderColumn1Snapshot() {
    const container = byId("snapshot-body");
    const cust = wb.dossier.customer_record ?? {};
    const riskTier = String(cust.risk_tier ?? "LOW");
    const riskPct = typeof wb.dossier.high_risk_probability === "number"
        ? `${Math.round(wb.dossier.high_risk_probability * 100)}%`
        : riskTier === "LOW"
            ? "12%"
            : riskTier === "MED"
                ? "38%"
                : "84%";
    const badgeClass = riskTier === "LOW"
        ? "badge--good"
        : riskTier === "MED"
            ? "badge--warn"
            : "badge--bad";
    let products = [];
    try {
        products =
            typeof cust.current_products === "string"
                ? JSON.parse(cust.current_products)
                : (cust.current_products ?? ["CHECKING_BASIC"]);
    }
    catch {
        products = ["CHECKING_BASIC"];
    }
    const checkingBal = Number(cust.avg_checking_balance ?? 0);
    const existingDebt = Number(cust.existing_debt ?? 0);
    container.innerHTML = `
    <h3 class="snap-name">${cust.full_name ?? "Customer"}</h3>
    <div class="snap-id">ID: #${cust.customer_id} · ${wb.role === "IN_APP" ? "In-App Channel" : "Branch Advisor"} (Pref: ${cust.preferred_channel ?? "IN_APP"})</div>

    <div class="snap-meta">
      <div class="snap-row">
        <span>Segment</span>
        <strong>${cust.customer_segment ?? "Mass_Affluent"}</strong>
      </div>
      <div class="snap-row">
        <span>Annual Income</span>
        <strong>${money(Number(cust.annual_income ?? 0))}</strong>
      </div>
      <div class="snap-row">
        <span>Credit Score</span>
        <strong>${cust.credit_score ?? 720}</strong>
      </div>
      <div class="snap-row">
        <span>Churn / Risk</span>
        <span class="badge ${badgeClass}">${riskTier} (${riskPct})</span>
      </div>
    </div>

    <div class="section-label">HOLDINGS (${products.length} of 3 Target)</div>
    <ul class="holdings-list">
      <li class="holding-item">
        <span>Checking Account</span>
        <strong>${money(checkingBal)}</strong>
      </li>
      <li class="holding-item">
        <span>Existing Debt / Loan</span>
        <strong>${existingDebt > 0 ? money(existingDebt) : "Paid Off"}</strong>
      </li>
      ${products
        .map((p) => `
        <li class="holding-item">
          <span>Active Product</span>
          <strong>${p}</strong>
        </li>`)
        .join("")}
    </ul>
  `;
}
/** COLUMN 2: RANKED NEXT BEST ACTIONS & AI SIGNALS */
function renderColumn2Offers() {
    const listEl = byId("offers-list");
    const signalsEl = byId("ai-signals");
    const subtitleEl = byId("governance-subtitle");
    const suppressedSummary = byId("suppressed-summary");
    const suppressedList = byId("suppressed-list");
    subtitleEl.textContent =
        wb.role === "IN_APP"
            ? "Policy-First & Expected Profit Ranked · In-App Self-Serve ($0.50 contact cost)"
            : "Policy-First & Expected Profit Ranked · Branch Advisor ($18.00 contact cost)";
    const offers = wb.dossier.candidate_offers ?? [];
    const suppressed = wb.dossier.suppressed_offers ?? [];
    const features = wb.dossier.accumulated_features ?? {};
    const cust = wb.dossier.customer_record ?? {};
    suppressedSummary.textContent = `Governance Audit: Suppressed Offers (${suppressed.length})`;
    suppressedList.innerHTML =
        suppressed.length === 0
            ? `<div class="suppressed-item">No offers suppressed.</div>`
            : suppressed
                .map((s) => `<div class="suppressed-item"><strong>${s.offer_name ?? s.offer_id}:</strong> ${s.reason}</div>`)
                .join("");
    if (wb.dossier.risk_blocked || offers.length === 0) {
        listEl.innerHTML = `
      <div class="offer-card offer-card--selected">
        <div class="offer-card__top">
          <span class="offer-card__title">⛔ Promotional Offers Suppressed</span>
          <span class="badge badge--bad">SERVICING ONLY</span>
        </div>
        <div class="offer-card__metrics">
          <span>${wb.dossier.governance_reason ?? "No eligible offers passed policy gates."}</span>
        </div>
      </div>
    `;
        signalsEl.innerHTML = `
      <li>Governance Policy Gate triggered: <strong>${wb.dossier.governance_reason ?? "Policy restriction"}</strong></li>
      <li>${wb.role === "IN_APP"
            ? "In-app promotional banners suppressed; self-service account support remains active."
            : "Advisor instructed to remain in non-promotional servicing mode."}</li>
    `;
        return;
    }
    listEl.innerHTML = offers
        .map((offer, idx) => {
        const selected = idx === wb.selectedOfferIdx;
        const pm = offer.profit_metrics ?? {};
        const propPct = Math.round((pm.prob_acceptance ?? 0.78) * 100);
        const estVal = channelExpectedProfit(offer);
        return `
        <div class="offer-card ${selected ? "offer-card--selected" : ""}" data-offer-idx="${idx}">
          <div class="offer-card__top">
            <span class="offer-card__title">#${idx + 1} ${offer.offer_name.toUpperCase()} (${propPct}%)</span>
            ${selected ? `<span class="offer-card__pill">Selected</span>` : ""}
          </div>
          <div class="offer-card__metrics">
            <span>Est. Value: <strong>${money(estVal)}/yr</strong></span>
            <span>Terms: ${offer.apr_range ?? "Standard"}</span>
          </div>
        </div>
      `;
    })
        .join("");
    listEl.querySelectorAll(".offer-card").forEach((card) => {
        card.addEventListener("click", () => {
            wb.selectedOfferIdx = Number(card.dataset.offerIdx ?? 0);
            renderAllPanels();
        });
    });
    // Populate AI Signals ("WHY THIS OFFER?")
    const bal = Number(cust.avg_checking_balance ?? 0);
    const signals = [];
    if (features.idle_cash_rate_gap) {
        signals.push(`Idle cash trigger: <strong>${features.idle_cash_rate_gap}</strong>`);
    }
    if (bal >= 15000) {
        signals.push(`Checking balance (${money(bal)}) is above the $15,000 high-yield threshold.`);
    }
    if (features.product_depth) {
        signals.push(`Relationship depth: <strong>${features.product_depth}</strong> toward 7-year retention goal.`);
    }
    if (features.debt_to_income_ratio) {
        signals.push(`Debt-to-income ratio verified at <strong>${features.debt_to_income_ratio}</strong>.`);
    }
    if (features.contact_eligibility) {
        signals.push(`Governance clearance: <strong>${features.contact_eligibility}</strong>.`);
    }
    signalsEl.innerHTML = signals.map((s) => `<li>${s}</li>`).join("");
}
/** COLUMN 3: CONVERSATION & ACTION PANEL */
function renderColumn3ActionPanel() {
    const activePill = byId("active-offer-pill");
    const talkingEl = byId("talking-points");
    const complianceEl = byId("compliance-footer");
    const simProjected = byId("sim-projected");
    const tabScript = byId("tab-btn-script");
    const btnPushApp = byId("btn-push-app");
    const btnOpenNow = byId("btn-open-now");
    const composerInput = byId("composer-input");
    const isInApp = wb.role === "IN_APP";
    tabScript.textContent = isInApp ? "In-App Offer View" : "Advisor Script";
    btnPushApp.textContent = isInApp
        ? "🔖 Save Offer for Later"
        : "📲 Push Offer to Customer App";
    btnOpenNow.textContent = isInApp
        ? "⚡ Apply & Activate in App"
        : "✅ Open Account Now";
    composerInput.placeholder = isInApp
        ? "Ask our AI banking assistant about your accounts or offers…"
        : "Simulate customer conversation…";
    const offers = wb.dossier.candidate_offers ?? [];
    const cust = wb.dossier.customer_record ?? {};
    const firstName = String(cust.full_name ?? "there").split(" ")[0];
    if (wb.dossier.risk_blocked || offers.length === 0) {
        activePill.textContent = "Active Offer: None (Servicing Mode)";
        talkingEl.textContent = isInApp
            ? `"Welcome back, ${firstName}. Your accounts are ready — use Live AI Chat or self-service tools to manage your banking preferences."`
            : `"Hi ${firstName}, thank you for banking with us today. How can I assist you with your existing accounts?"`;
        complianceEl.textContent =
            "Compliance Lock: Promotional cross-sell suppressed by enterprise governance rules.";
        simProjected.textContent = "$0.00";
        return;
    }
    const offer = offers[wb.selectedOfferIdx] ?? offers[0];
    activePill.textContent = `Active Offer: ${offer.offer_name}`;
    // Calculate live simulator yield/savings based on product category
    const amt = wb.simAmount;
    let rate = 0.0465;
    let metricLabel = "interest";
    if (offer.product_category === "LENDING") {
        rate = 0.085;
        metricLabel = "annual interest savings";
    }
    else if (offer.product_category?.startsWith("CARD")) {
        rate = 0.025;
        metricLabel = "annual rewards value";
    }
    else if (offer.product_category === "WEALTH") {
        rate = 0.068;
        metricLabel = "projected net portfolio growth";
    }
    const projected = amt * rate;
    simProjected.textContent = `+${money(projected, 2)}`;
    talkingEl.textContent = isInApp
        ? `"${firstName}, earn an estimated ${money(projected, 2)}/yr in ${metricLabel} when you allocate ${money(amt)} to ${offer.offer_name}. ${offer.cta_text ?? "Tap below to activate in seconds."}"`
        : `"Hi ${firstName}, based on your ${cust.customer_segment ?? "profile"} relationship, moving ${money(amt)} into our ${offer.offer_name} earns ${money(projected, 2)}/yr in ${metricLabel}. ${offer.cta_text ?? "Would you like to activate this today?"}"`;
    complianceEl.textContent =
        offer.approved_compliance_template ??
            "COMPLIANCE DISCLOSURE: Terms and rates subject to final eligibility verification.";
}
function syncCohortFilterPill() {
    const pill = byId("cohort-filter-pill");
    if (wb.cohortFilterLabel && wb.activeView === "WORKBENCH") {
        pill.textContent = `Cohort: ${wb.cohortFilterLabel} ×`;
        pill.classList.remove("hidden");
    }
    else {
        pill.classList.add("hidden");
    }
}
function setWorkspaceView(view) {
    wb.activeView = view;
    const btnWorkbench = byId("view-btn-workbench");
    const btnAnalytics = byId("view-btn-analytics");
    const workbenchEl = byId("workbench-view");
    const portfolioEl = byId("portfolio-view");
    const customerControls = byId("topbar-customer-controls");
    const hintEl = byId("customer-hint");
    const isAnalytics = view === "ANALYTICS";
    btnWorkbench.classList.toggle("view-switcher__btn--active", !isAnalytics);
    btnWorkbench.setAttribute("aria-selected", isAnalytics ? "false" : "true");
    btnAnalytics.classList.toggle("view-switcher__btn--active", isAnalytics);
    btnAnalytics.setAttribute("aria-selected", isAnalytics ? "true" : "false");
    workbenchEl.classList.toggle("hidden", isAnalytics);
    portfolioEl.classList.toggle("hidden", !isAnalytics);
    customerControls.classList.toggle("hidden", isAnalytics);
    hintEl.classList.toggle("hidden", isAnalytics);
    syncCohortFilterPill();
    if (isAnalytics) {
        void loadPortfolioAnalytics();
    }
}
async function loadPortfolioAnalytics() {
    const subtitleEl = document.getElementById("portfolio-subtitle");
    if (subtitleEl && !wb.portfolioData) {
        subtitleEl.textContent = "Computing governance & Expected Profit telemetry across customer book…";
    }
    try {
        const data = await fetchPortfolioAnalytics(wb.role);
        wb.portfolioData = data;
        renderPortfolioDashboard(data, {
            onSelectSegment: (seg) => {
                const profitableOnly = seg.profitable_customers > 0;
                const label = profitableOnly
                    ? `${seg.label} · Profitable (${seg.profitable_customers.toLocaleString("en-US")})`
                    : `${seg.label} · Guardrail Review`;
                void applyCohortDrilldown({
                    segment: seg.segment,
                    profitable_only: profitableOnly,
                    channel: wb.role,
                    limit: 50,
                }, label);
            },
            onSelectOffer: (offer) => {
                const label = `#1 NBA: ${offer.offer_name}`;
                void applyCohortDrilldown({
                    offer_id: offer.offer_id,
                    profitable_only: true,
                    channel: wb.role,
                    limit: 50,
                }, label);
            },
        });
    }
    catch (err) {
        showToast(`Portfolio analytics failed: ${err instanceof Error ? err.message : String(err)}`, "error");
    }
}
async function applyCohortDrilldown(params, label) {
    try {
        const cohortCustomers = await fetchCustomers(params);
        if (cohortCustomers.length === 0) {
            showToast(`No matching customers found for ${label}.`, "error");
            return;
        }
        const searchInput = byId("customer-search");
        searchInput.value = "";
        wb.cohortFilterLabel = label;
        state.customers = cohortCustomers;
        wb.filteredCustomers = cohortCustomers;
        populateCustomerSelect(cohortCustomers);
        const first = cohortCustomers[0];
        if (first) {
            state.customerId = Number(first.customer_id);
            byId("customer-select").value = String(state.customerId);
            resetConversation();
            repaintChat();
        }
        setWorkspaceView("WORKBENCH");
        if (state.customerId !== null) {
            await loadCustomerDossier(state.customerId);
        }
        showToast(`Filtered to ${cohortCustomers.length} profiles · ${label}`);
    }
    catch (err) {
        showToast(`Cohort filter failed: ${err instanceof Error ? err.message : String(err)}`, "error");
    }
}
function clearCohortFilter(reloadDossier = true) {
    wb.cohortFilterLabel = null;
    state.customers = wb.defaultCustomers;
    wb.filteredCustomers = wb.defaultCustomers;
    byId("customer-search").value = "";
    populateCustomerSelect(wb.defaultCustomers);
    syncCohortFilterPill();
    const first = wb.defaultCustomers[0];
    if (reloadDossier && first) {
        state.customerId = Number(first.customer_id);
        byId("customer-select").value = String(state.customerId);
        resetConversation();
        repaintChat();
        void loadCustomerDossier(state.customerId);
    }
}
function wireWorkbenchEvents() {
    const searchInput = byId("customer-search");
    const selectEl = byId("customer-select");
    const roleSelect = byId("role-select");
    const simInput = byId("sim-amount");
    const btnViewWorkbench = byId("view-btn-workbench");
    const btnViewAnalytics = byId("view-btn-analytics");
    const cohortPill = byId("cohort-filter-pill");
    wb.role = roleSelect.value === "IN_APP" ? "IN_APP" : "BANKER_DESKTOP";
    btnViewWorkbench.addEventListener("click", () => {
        setWorkspaceView("WORKBENCH");
    });
    btnViewAnalytics.addEventListener("click", () => {
        setWorkspaceView("ANALYTICS");
    });
    cohortPill.addEventListener("click", () => {
        clearCohortFilter(true);
        showToast("Cleared portfolio cohort filter.");
    });
    // Filter customer list live as advisor types
    searchInput.addEventListener("input", () => {
        const q = searchInput.value.trim().toLowerCase().replace(/^#/, "");
        wb.filteredCustomers = !q
            ? state.customers
            : state.customers.filter((c) => c.full_name.toLowerCase().includes(q) ||
                String(c.customer_id).includes(q) ||
                String(c.customer_segment ?? "")
                    .toLowerCase()
                    .includes(q));
        populateCustomerSelect(wb.filteredCustomers);
        if (wb.filteredCustomers.length === 1) {
            const onlyId = Number(wb.filteredCustomers[0]?.customer_id);
            if (onlyId) {
                state.customerId = onlyId;
                resetConversation();
                repaintChat();
                void loadCustomerDossier(onlyId);
            }
        }
    });
    // Pressing Enter in customer search selects the top matching customer
    searchInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && wb.filteredCustomers.length > 0) {
            e.preventDefault();
            const topId = Number(wb.filteredCustomers[0]?.customer_id);
            if (!topId)
                return;
            state.customerId = topId;
            selectEl.value = String(topId);
            resetConversation();
            repaintChat();
            void loadCustomerDossier(topId);
        }
    });
    // Switching dropdown immediately loads all 3 columns
    selectEl.addEventListener("change", () => {
        state.customerId = Number(selectEl.value);
        resetConversation();
        repaintChat();
        void loadCustomerDossier(state.customerId);
    });
    // Role switcher (Branch Advisor Desktop vs. Customer In-App)
    roleSelect.addEventListener("change", () => {
        wb.role = roleSelect.value === "IN_APP" ? "IN_APP" : "BANKER_DESKTOP";
        if (wb.activeView === "ANALYTICS") {
            void loadPortfolioAnalytics();
        }
        if (state.customerId !== null) {
            void loadCustomerDossier(state.customerId);
        }
        else {
            renderAllPanels();
        }
        showToast(wb.role === "IN_APP"
            ? "Switched to Customer In-App economics ($0.50 contact cost)."
            : "Switched to Branch Advisor economics ($18.00 contact cost).");
    });
    // Live Quick Simulator recalculation
    simInput.addEventListener("input", () => {
        wb.simAmount = Math.max(0, Number(simInput.value || 0));
        renderColumn3ActionPanel();
    });
    // Execution buttons
    byId("btn-push-app").addEventListener("click", () => {
        const offer = wb.dossier?.candidate_offers?.[wb.selectedOfferIdx];
        if (!offer) {
            showToast("No active offer available.");
            return;
        }
        showToast(wb.role === "IN_APP"
            ? `🔖 Saved "${offer.offer_name}" to your in-app offers wallet.`
            : `📲 Pushed "${offer.offer_name}" to ${wb.dossier.customer_record?.full_name}'s mobile app.`);
    });
    byId("btn-open-now").addEventListener("click", () => {
        const offer = wb.dossier?.candidate_offers?.[wb.selectedOfferIdx];
        if (!offer) {
            showToast("No active offer selected.");
            return;
        }
        showToast(wb.role === "IN_APP"
            ? `⚡ Self-service application started for ${offer.offer_name}.`
            : `✅ Origination workflow launched for ${offer.offer_name}.`);
    });
    // Outcome Radio Logger -> POST /api/outcomes
    document
        .querySelectorAll('input[name="outcome"]')
        .forEach((radio) => {
        radio.addEventListener("change", async () => {
            const offer = wb.dossier?.candidate_offers?.[wb.selectedOfferIdx];
            if (!offer || !wb.dossier?.customer_record)
                return;
            const outcome = radio.value;
            try {
                await fetch("/api/outcomes", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        customer_id: wb.dossier.customer_record.customer_id,
                        offer_id: offer.offer_id,
                        outcome,
                        channel: wb.role,
                        simulated_amount: wb.simAmount,
                    }),
                });
            }
            catch {
                // Non-blocking in demo mode
            }
            byId("outcome-feedback").textContent =
                `✓ Logged "${outcome}" for ${offer.offer_name} (feeds AI retraining loop).`;
            showToast(`Outcome logged: ${outcome}`);
        });
    });
    // Tab switcher (Advisor Script vs. Live AI Chat)
    const tabScript = byId("tab-btn-script");
    const tabChat = byId("tab-btn-chat");
    const paneScript = byId("pane-script");
    const paneChat = byId("pane-chat");
    tabScript.addEventListener("click", () => {
        tabScript.classList.add("tab--active");
        tabScript.setAttribute("aria-selected", "true");
        tabChat.classList.remove("tab--active");
        tabChat.setAttribute("aria-selected", "false");
        paneScript.classList.remove("hidden");
        paneChat.classList.add("hidden");
    });
    tabChat.addEventListener("click", () => {
        tabChat.classList.add("tab--active");
        tabChat.setAttribute("aria-selected", "true");
        tabScript.classList.remove("tab--active");
        tabScript.setAttribute("aria-selected", "false");
        paneChat.classList.remove("hidden");
        paneScript.classList.add("hidden");
        repaintChat();
    });
    // Alerts button
    byId("alerts-btn").addEventListener("click", () => {
        showToast("🔔 2 Real-Time Signals: Idle checking balance > $15k & Pre-approved 2nd product eligibility.");
    });
    // Reset & Theme (single reset listener)
    byId("new-conversation").addEventListener("click", () => {
        if (wb.cohortFilterLabel) {
            clearCohortFilter(false);
        }
        else {
            searchInput.value = "";
            wb.filteredCustomers = state.customers;
            populateCustomerSelect(wb.filteredCustomers);
        }
        resetConversation();
        repaintChat();
        if (wb.activeView === "ANALYTICS") {
            void loadPortfolioAnalytics();
        }
        else if (state.customerId !== null) {
            void loadCustomerDossier(state.customerId);
        }
        showToast("Workbench reset.");
    });
    const themeBtn = byId("theme-toggle");
    syncToggleButton(themeBtn, currentTheme());
    themeBtn.addEventListener("click", () => {
        syncToggleButton(themeBtn, toggleTheme());
    });
    // Preserved LangGraph Chat Composer (Enter submits chat; Shift+Enter adds newline)
    const form = byId("composer");
    const input = byId("composer-input");
    const sendComposerMessage = () => {
        if (state.pending)
            return;
        const msg = input.value.trim();
        if (!msg)
            return;
        input.value = "";
        void submitChatTurn(msg);
    };
    form.addEventListener("submit", (e) => {
        e.preventDefault();
        sendComposerMessage();
    });
    input.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
            e.preventDefault();
            sendComposerMessage();
        }
    });
}
function repaintChat() {
    const chatEl = document.getElementById("chat");
    if (!chatEl)
        return;
    if (!Array.isArray(state.history)) {
        state.history = [];
    }
    try {
        renderChat(chatEl, (text) => {
            void submitChatTurn(text);
        });
    }
    catch (err) {
        console.warn("Chat render skipped:", err);
    }
}
async function submitChatTurn(message) {
    if (state.pending)
        return;
    state.history.push({ role: "user", content: message, at: new Date(), runId: null });
    state.pending = true;
    repaintChat();
    try {
        const result = await postTurn(message, state.threadId, state.customerId);
        state.history.push({
            role: "assistant",
            content: result.display_message || result.assistant_message,
            at: new Date(),
            runId: result.run_id,
        });
        state.lastResult = result;
    }
    catch (err) {
        const detail = err instanceof ApiError
            ? `${err.message} (HTTP ${err.status})`
            : err instanceof Error
                ? err.message
                : String(err);
        state.history.push({
            role: "assistant",
            content: `Turn error: ${detail}`,
            at: new Date(),
            runId: null,
            isError: true,
        });
    }
    finally {
        state.pending = false;
        repaintChat();
    }
}
async function boot() {
    wireWorkbenchEvents();
    repaintChat();
    const select = byId("customer-select");
    select.innerHTML = `<option value="">Loading customers...</option>`;
    try {
        const [config, rawCustomers] = await Promise.all([
            fetchConfig(),
            fetchCustomers(),
        ]);
        state.config = config;
        // Handle both [...] and { customers: [...] } shapes
        const customers = Array.isArray(rawCustomers)
            ? rawCustomers
            : Array.isArray(rawCustomers?.customers)
                ? rawCustomers.customers
                : [];
        wb.defaultCustomers = customers;
        state.customers = customers;
        wb.filteredCustomers = customers;
        populateCustomerSelect(customers);
        const first = customers[0];
        if (first) {
            const firstId = Number(first.customer_id);
            state.customerId = firstId;
            await loadCustomerDossier(firstId);
        }
        else {
            select.innerHTML = `<option value="">0 customers in DB</option>`;
        }
    }
    catch (err) {
        console.error("Workbench boot failed:", err);
        select.innerHTML = `<option value="">API Error — check server on port 8000</option>`;
        showToast(`API unreachable: ${err instanceof Error ? err.message : String(err)}`, "error");
    }
}
void boot();
//# sourceMappingURL=main.js.map