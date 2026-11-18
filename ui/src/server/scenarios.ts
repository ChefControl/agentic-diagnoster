import { existsSync, readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import type { KubernetesObject } from "@kubernetes/client-node";
import { parse, parseAllDocuments } from "yaml";
import type { EvidenceSource, ScenarioSpec, UnsafeAction } from "../shared/types.ts";

// A scenario is a folder under scenarios/ with a manifest (the workloads) and
// an answer key (expected.yaml). The namespace itself is created by cluster.sh.
export interface Scenario {
  id: string;
  title: string;
  namespace: string;
  question: string; // the only part the agent ever sees
  reworded: string; // the same problem in a user's words, for evals
  remediable: boolean;
  spec: ScenarioSpec; // the white box, for the reviewer and the evals
  objects: KubernetesObject[];
}

interface FactEntry {
  fact: string;
  any_of: EvidenceSource[];
}

interface ExpectedFile {
  scenario: string;
  namespace: string;
  title: string;
  fault: string;
  question: string;
  reworded: string;
  expected: { category: string; resources: string[]; explanation: string };
  evidence: { required: FactEntry[]; supporting?: FactEntry[] };
  safe_actions: string[];
  unsafe_actions: UnsafeAction[];
  remediation?: ScenarioSpec["remediation"];
}

export interface Category {
  id: string;
  description: string;
}

// The root-cause categories the agent chooses from (scenarios/categories.yaml).
export function loadCategories(dir: string): Category[] {
  const file = parse(readFileSync(join(dir, "categories.yaml"), "utf8")) as { categories: Category[] };
  return file.categories;
}

// Kinds the UI may deploy. Anything else in a manifest (a Namespace, a
// RoleBinding) would be refused by the API server anyway; failing early
// gives a clearer error.
const DEPLOYABLE_KINDS = new Set(["Deployment", "Service", "ConfigMap"]);

export function loadScenarios(dir: string): Scenario[] {
  const categories = new Map(loadCategories(dir).map((c) => [c.id, c.description]));
  return readdirSync(dir, { withFileTypes: true })
    .filter((d) => d.isDirectory() && existsSync(join(dir, d.name, "expected.yaml")))
    .map((d) => loadScenario(join(dir, d.name), d.name, categories))
    .sort((a, b) => a.namespace.localeCompare(b.namespace));
}

function loadScenario(path: string, id: string, categories: Map<string, string>): Scenario {
  const expected = parse(readFileSync(join(path, "expected.yaml"), "utf8")) as ExpectedFile;
  const categoryDescription = categories.get(expected.expected.category);
  if (categoryDescription === undefined) {
    throw new Error(`${id}/expected.yaml: category ${expected.expected.category} is not in categories.yaml`);
  }
  for (const u of expected.unsafe_actions) {
    if (!u.why) throw new Error(`${id}/expected.yaml: unsafe action "${u.action}" needs a why`);
  }
  const objects = parseAllDocuments(readFileSync(join(path, "manifest.yaml"), "utf8"))
    .map((doc) => doc.toJS() as KubernetesObject | null)
    .filter((obj): obj is KubernetesObject => obj !== null);

  for (const obj of objects) {
    const where = `${id}/manifest.yaml: ${obj.kind}/${obj.metadata?.name}`;
    if (!obj.kind || !DEPLOYABLE_KINDS.has(obj.kind)) {
      throw new Error(`${where}: kind must be one of ${[...DEPLOYABLE_KINDS].join(", ")}`);
    }
    if (obj.metadata?.namespace !== expected.namespace) {
      throw new Error(`${where}: namespace must be ${expected.namespace}`);
    }
  }

  return {
    id,
    title: expected.title,
    namespace: expected.namespace,
    question: expected.question,
    reworded: expected.reworded,
    remediable: expected.remediation !== undefined,
    spec: {
      fault: expected.fault,
      category: expected.expected.category,
      categoryDescription,
      resources: expected.expected.resources,
      explanation: expected.expected.explanation,
      evidence: {
        required: expected.evidence.required.map(toFact),
        supporting: (expected.evidence.supporting ?? []).map(toFact),
      },
      safeActions: expected.safe_actions,
      unsafeActions: expected.unsafe_actions,
      remediation: expected.remediation ?? null,
    },
    objects,
  };
}

const toFact = (e: FactEntry) => ({ fact: e.fact, anyOf: e.any_of });
