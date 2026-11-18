// Scenario commands for CI and quick local use. Same code path as the UI.
//
//   npm run scenarios -- deploy <id|all>
//   npm run scenarios -- check  <id|all> [--wait <seconds>]
//   npm run scenarios -- reset  <id|all>
//   npm run scenarios -- remove <id|all>
import { config } from "./config.ts";
import { describeClusterError } from "./kube.ts";
import { loadScenarios } from "./scenarios.ts";
import { ScenarioService } from "./service.ts";
import type { ScenarioStatus } from "../shared/types.ts";

const service = new ScenarioService(loadScenarios(config.scenariosDir), config.scenarioManagerKubeconfig);
const [command, target, ...rest] = process.argv.slice(2);
const waitIndex = rest.indexOf("--wait");
const waitSeconds = waitIndex >= 0 ? Number(rest[waitIndex + 1]) : 0;

const green = (s: string) => `\x1b[32m${s}\x1b[0m`;
const red = (s: string) => `\x1b[31m${s}\x1b[0m`;

async function check(id: string): Promise<boolean> {
  const deadline = Date.now() + waitSeconds * 1000;
  let status: ScenarioStatus;
  do {
    status = await service.status(id);
    if (status.state === "reproduced") break;
    await new Promise((r) => setTimeout(r, 3000));
  } while (Date.now() < deadline);

  console.log(`${id} (namespace ${status.namespace}): ${status.state}`);
  for (const c of status.checks) console.log(`  ${c.pass ? green("✓") : red("✗")} ${c.label}`);
  for (const c of status.changes) console.log(`  ${red("changed")} ${c.resource} ${c.field} (by ${c.by})`);
  return status.state === "reproduced";
}

async function main(): Promise<number> {
  if (!command || !target || !["deploy", "check", "reset", "remove"].includes(command)) {
    console.error("usage: npm run scenarios -- <deploy|check|reset|remove> <id|all> [--wait <seconds>]");
    console.error(`scenarios: ${service.ids().join(", ")}`);
    return 2;
  }
  const ids = target === "all" ? service.ids() : [service.get(target).id];
  let ok = true;
  for (const id of ids) {
    if (command === "check") ok = (await check(id)) && ok;
    else {
      await service[command as "deploy" | "reset" | "remove"](id);
      console.log(`${green("✓")} ${command} ${id}`);
    }
  }
  return ok ? 0 : 1;
}

main().then(
  (code) => process.exit(code),
  (err) => {
    console.error(red(describeClusterError(err) ?? (err instanceof Error ? err.message : String(err))));
    process.exit(1);
  },
);
