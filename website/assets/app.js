/* GBM Cloud interactions stay in the browser; no analytics or backend calls. */
"use strict";

const PLAN_STORAGE_KEY = "gbm-cloud-launch-plan-v1";
// Compatibility only: retain access to previously saved browser data.
const LEGACY_STORAGE_KEY = "field-notes-plan-v1";

function checklistItems(scope, focus) {
  const items = [
    "Define the project’s purpose and pages",
    "Check mobile navigation and assets",
    "Keep credentials out of source control",
    "Store content in a private S3 bucket",
    "Connect CloudFront with origin access control",
    "Enable HTTPS with an ACM certificate",
    "Record a repeatable deployment process",
    "Save a rollback plan",
  ];
  if (scope === "launch") {
    items.push("Validate the custom domain in DNS", "Test HTTP-to-HTTPS redirects", "Check anonymous S3 access is denied");
  }
  if (focus === "performance") {
    items.push("Review asset sizes and caching", "Invalidate changed content after publishing");
  }
  if (focus === "security") {
    items.push("Review bucket policy scope", "Check least-privilege permissions");
  }
  return items;
}

function normalizePlan(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const name = typeof value.name === "string" ? value.name : typeof value.outing === "string" ? value.outing : "";
  const scope = value.scope === "launch" || value.duration === "day" ? "launch" : "prototype";
  const oldFocus = { sunny: "performance", rainy: "security", mild: "balanced" }[value.weather];
  const focus = ["balanced", "performance", "security"].includes(value.focus) ? value.focus : oldFocus || "balanced";
  const allowed = checklistItems(scope, focus);
  const checked = Array.isArray(value.checked)
    ? [...new Set(value.checked.filter((item) => typeof item === "string" && allowed.includes(item)))]
    : [];
  return { name: name.replace(/[\u0000-\u001f]/g, " ").slice(0, 80), scope, focus, checked };
}

function matchesSolution(card, category, level) {
  return (category === "all" || card.category === category) && (level === "all" || card.level === level);
}

function selectedPlan(value, requested) {
  const plan = normalizePlan(value) || normalizePlan({});
  if (typeof requested === "string" && requested.trim()) {
    const name = requested.replace(/[\u0000-\u001f]/g, " ").slice(0, 80).trim();
    if (name !== plan.name) return { ...plan, name, checked: [] };
  }
  return plan;
}

function checklistText(state) {
  return [
    "GBM Cloud — Launch checklist",
    state.name || "My cloud project",
    "Stage: " + (state.scope === "launch" ? "Public launch" : "Prototype"),
    "Focus: " + state.focus,
    "",
    ...checklistItems(state.scope, state.focus).map((item) => "[" + (state.checked.includes(item) ? "x" : " ") + "] " + item),
    "",
    "A personal planning checklist. Validate the actual deployment before sharing it.",
  ].join("\n");
}

function initializeSite(doc, win) {
  doc.documentElement.classList.add("js");
  const menuButton = doc.querySelector(".menu-toggle");
  const navigation = doc.querySelector("#main-nav");
  function setMenu(open) {
    if (!menuButton || !navigation) return;
    menuButton.setAttribute("aria-expanded", String(open));
    menuButton.setAttribute("aria-label", open ? "Close navigation" : "Open navigation");
    navigation.classList.toggle("is-open", open);
  }
  if (menuButton && navigation) {
    menuButton.addEventListener("click", () => setMenu(menuButton.getAttribute("aria-expanded") !== "true"));
    navigation.addEventListener("click", (event) => {
      if (event.target.closest("a")) setMenu(false);
    });
    doc.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && menuButton.getAttribute("aria-expanded") === "true") {
        setMenu(false);
        menuButton.focus();
      }
    });
    doc.addEventListener("click", (event) => {
      if (!menuButton.contains(event.target) && !navigation.contains(event.target)) setMenu(false);
    });
    const desktop = win.matchMedia?.("(min-width: 761px)");
    desktop?.addEventListener?.("change", () => setMenu(false));
  }

  const category = doc.querySelector("#solution-category");
  const level = doc.querySelector("#solution-level");
  if (category && level) {
    const cards = [...doc.querySelectorAll("[data-solution]")];
    function filterSolutions() {
      let count = 0;
      cards.forEach((card) => {
        const visible = matchesSolution(card.dataset, category.value, level.value);
        card.hidden = !visible;
        if (visible) count += 1;
      });
      doc.querySelector("#solution-count").textContent = count + (count === 1 ? " solution to explore" : " solutions to explore");
      doc.querySelector("#solution-empty").hidden = count !== 0;
    }
    category.addEventListener("change", filterSolutions);
    level.addEventListener("change", filterSolutions);
    doc.querySelector("#reset-filters").addEventListener("click", () => {
      category.value = "all";
      level.value = "all";
      filterSolutions();
    });
    filterSolutions();
  }

  const planner = doc.querySelector("#launch-planner");
  if (!planner) return;
  const nameInput = doc.querySelector("#project-name");
  const scopeInput = doc.querySelector("#project-scope");
  const focusInput = doc.querySelector("#project-focus");
  const list = doc.querySelector("#launch-checklist");
  const status = doc.querySelector("#planner-status");
  let storageFailed = false;
  let stored = null;
  try {
    const raw = win.localStorage.getItem(PLAN_STORAGE_KEY) || win.localStorage.getItem(LEGACY_STORAGE_KEY);
    if (raw) stored = JSON.parse(raw);
  } catch {
    storageFailed = true;
    status.textContent = "Saved data is unavailable. Your checklist still works for this visit.";
  }
  const query = new URLSearchParams(win.location.search);
  // Old selection links continue to prefill the project name.
  const requested = query.get("solution") || query.get("trail");
  let state = selectedPlan(stored, requested);
  nameInput.value = state.name;
  scopeInput.value = state.scope;
  focusInput.value = state.focus;

  function save() {
    try {
      win.localStorage.setItem(PLAN_STORAGE_KEY, JSON.stringify(state));
      storageFailed = false;
      return true;
    } catch {
      storageFailed = true;
      status.textContent = "This browser cannot save your plan. Download it to keep a copy.";
      return false;
    }
  }
  function progress() {
    const items = checklistItems(state.scope, state.focus);
    const count = items.filter((item) => state.checked.includes(item)).length;
    doc.querySelector("#checklist-progress").textContent = count + " / " + items.length + " checked";
    doc.querySelector("#progress-fill").style.width = (count / items.length * 100) + "%";
  }
  function render() {
    doc.querySelector("#list-title").textContent = state.name || "Your launch checklist.";
    list.replaceChildren();
    checklistItems(state.scope, state.focus).forEach((item) => {
      const row = doc.createElement("li");
      const label = doc.createElement("label");
      const checkbox = doc.createElement("input");
      const text = doc.createElement("span");
      text.textContent = item;
      checkbox.type = "checkbox";
      checkbox.checked = state.checked.includes(item);
      checkbox.addEventListener("change", () => {
        state.checked = state.checked.filter((value) => value !== item);
        if (checkbox.checked) state.checked.push(item);
        save();
        progress();
      });
      label.append(checkbox, text);
      row.append(label);
      list.append(row);
    });
    progress();
  }
  planner.addEventListener("submit", (event) => {
    event.preventDefault();
    state = normalizePlan({ name: nameInput.value.trim(), scope: scopeInput.value, focus: focusInput.value, checked: state.checked });
    nameInput.value = state.name;
    if (save()) status.textContent = "Your launch checklist is ready and saved on this device.";
    render();
  });
  planner.addEventListener("input", () => {
    if (!storageFailed) status.textContent = "Build your checklist to apply these project details.";
  });
  doc.querySelector("#download-list").addEventListener("click", () => {
    const url = win.URL.createObjectURL(new Blob([checklistText(state)], { type: "text/plain;charset=utf-8" }));
    const anchor = doc.createElement("a");
    anchor.href = url;
    anchor.download = "gbm-cloud-launch-checklist.txt";
    doc.body.append(anchor);
    anchor.click();
    anchor.remove();
    win.setTimeout(() => win.URL.revokeObjectURL(url), 1000);
    if (!storageFailed) status.textContent = "Your checklist download is ready.";
  });
  doc.querySelector("#clear-plan").addEventListener("click", () => {
    state = normalizePlan({});
    planner.reset();
    nameInput.value = "";
    scopeInput.value = state.scope;
    focusInput.value = state.focus;
    try {
      win.localStorage.removeItem(PLAN_STORAGE_KEY);
      win.localStorage.removeItem(LEGACY_STORAGE_KEY);
      storageFailed = false;
    } catch {
      storageFailed = true;
    }
    // Clearing must survive a reload of a previously selected solution.
    try { win.history.replaceState(null, "", win.location.pathname + win.location.hash); } catch { /* Current checklist still resets. */ }
    status.textContent = storageFailed ? "Checklist cleared for this visit. Browser storage could not be updated." : "Saved plan cleared.";
    render();
  });
  if (requested) save();
  render();
}

if (typeof document !== "undefined" && typeof window !== "undefined") initializeSite(document, window);
if (typeof module !== "undefined") module.exports = { checklistItems, normalizePlan, matchesSolution, selectedPlan, checklistText, initializeSite, PLAN_STORAGE_KEY };
