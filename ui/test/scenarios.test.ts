import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { SCENARIO_CHECKS } from "../src/server/checks.ts";
import { loadCategories, loadScenarios } from "../src/server/scenarios.ts";

const dir = resolve(import.meta.dirname, "../../scenarios");
const scenarios = loadScenarios(dir);
const categories = loadCategories(dir).map((c) => c.id);

describe("scenario definitions", () => {
  it("loads all four scenarios", () => {
    expect(scenarios.map((s) => s.id).sort()).toEqual(["bad-probe", "healthy", "missing-config", "selector-mismatch"]);
  });

  it.each(scenarios)("$id keeps every object in its own namespace", (s) => {
    expect(s.objects.length).toBeGreaterThan(0);
    for (const obj of s.objects) expect(obj.metadata?.namespace).toBe(s.namespace);
  });

  it.each(scenarios)("$id does not give the answer away in its namespace or question", (s) => {
    const words = s.id.split("-");
    for (const w of words) {
      expect(s.namespace).not.toContain(w);
      expect(s.question.toLowerCase()).not.toContain(w === "bad" ? "bad probe" : w);
    }
  });

  it.each(scenarios)("$id has checks and an answer key", (s) => {
    expect(SCENARIO_CHECKS[s.id]?.checks.length).toBeGreaterThan(0);
    expect(categories).toContain(s.spec.category);
  });

  it("gives the agent more categories than there are scenarios", () => {
    // With one category per scenario, picking the category would be a lookup.
    expect(categories.length).toBeGreaterThanOrEqual(2 * scenarios.length);
    expect(new Set(categories).size).toBe(categories.length);
    expect(categories).toContain("none");
  });

  it.each(scenarios)("$id names a faulty resource unless nothing is broken", (s) => {
    if (s.spec.category === "none") expect(s.spec.resources).toEqual([]);
    else expect(s.spec.resources.length).toBeGreaterThan(0);
  });

  it.each(scenarios)("$id grades on required evidence and explains every unsafe action", (s) => {
    expect(s.spec.evidence.required.length).toBeGreaterThan(0);
    expect(s.spec.safeActions.length).toBeGreaterThan(0);
    for (const u of s.spec.unsafeActions) expect(u.why.length).toBeGreaterThan(0);
  });

  it.each(scenarios)("$id shows the reviewer the fault but keeps it out of the agent's question", (s) => {
    expect(s.title.length).toBeGreaterThan(0);
    expect(s.spec.fault.length).toBeGreaterThan(0);
    expect(s.question).not.toContain(s.spec.fault);
    expect(s.question.toLowerCase()).not.toContain(s.title.toLowerCase());
  });

  it("only the readiness-probe scenario is remediable", () => {
    expect(scenarios.filter((s) => s.remediable).map((s) => s.id)).toEqual(["bad-probe"]);
  });

  it("remediates a resource the answer key names as faulty", () => {
    for (const s of scenarios.filter((s) => s.spec.remediation)) {
      expect(s.spec.resources).toContain(s.spec.remediation?.target);
    }
  });
});
