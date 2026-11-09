const { test } = require("node:test");
const assert = require("node:assert/strict");
const { checklistItems, normalizePlan, matchesSolution, selectedPlan, checklistText } = require("../website/assets/app.js");

test("a public launch adds DNS, redirect and origin-access checks", () => {
  const basic = checklistItems("prototype", "balanced");
  const launch = checklistItems("launch", "security");
  assert.ok(basic.every((item) => launch.includes(item)));
  assert.ok(launch.includes("Validate the custom domain in DNS"));
  assert.ok(launch.includes("Review bucket policy scope"));
  assert.equal(launch.length, 13);
  assert.equal(new Set(launch).size, launch.length);
});
test("each focus contributes the appropriate additional tasks", () => {
  assert.ok(checklistItems("prototype", "performance").includes("Review asset sizes and caching"));
  assert.ok(!checklistItems("prototype", "balanced").includes("Review bucket policy scope"));
});
test("untrusted stored settings are bounded, deduplicated and normalized", () => {
  assert.equal(normalizePlan(null), null);
  assert.equal(normalizePlan([]), null);
  const valid = checklistItems("prototype", "balanced")[0];
  const plan = normalizePlan({ name: "x".repeat(1000), scope: "bad", focus: "bad", checked: [2, valid, valid, "<img src=x>"] });
  assert.equal(plan.name.length, 80);
  assert.equal(plan.scope, "prototype");
  assert.equal(plan.focus, "balanced");
  assert.deepEqual(plan.checked, [valid]);
});
test("saved project details can be migrated without accepting stale checklist entries", () => {
  const plan = normalizePlan({ outing: "My saved project", duration: "day", weather: "rainy", checked: ["Drinking water"] });
  assert.equal(plan.name, "My saved project");
  assert.equal(plan.scope, "launch");
  assert.equal(plan.focus, "security");
  assert.deepEqual(plan.checked, []);
});
test("reopening the same selected solution preserves completion state", () => {
  const item = checklistItems("prototype", "balanced")[0];
  const plan = { name: "Static website", scope: "prototype", focus: "balanced", checked: [item] };
  assert.deepEqual(selectedPlan(plan, "Static website").checked, [item]);
  assert.deepEqual(selectedPlan(plan, "Private origin").checked, []);
  assert.equal(selectedPlan(plan, null).name, "Static website");
});
test("solution filters combine category and difficulty and allow an empty result", () => {
  const card = { category: "security", level: "intermediate" };
  assert.ok(matchesSolution(card, "all", "all"));
  assert.ok(matchesSolution(card, "security", "intermediate"));
  assert.ok(!matchesSolution(card, "web", "intermediate"));
  assert.ok(!matchesSolution(card, "security", "beginner"));
});
test("downloaded checklists use current branding and reflect checked items", () => {
  const state = normalizePlan({ name: "My launch", scope: "launch", focus: "security", checked: [checklistItems("launch", "security")[0]] });
  const text = checklistText(state);
  assert.ok(text.includes("GBM Cloud"));
  assert.ok(text.includes("My launch"));
  assert.ok(text.includes("[x] Define"));
  assert.ok(!text.toLowerCase().includes("field notes"));
});
