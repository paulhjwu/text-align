# Starting n8n for the refine-alignment workflows

The workflows in this repo (`n8n-refine-alignment-orchestrator.json`,
`n8n-refine-retry-chapter.json`) use `n8n-nodes-base.executeCommand` to shell out to
`poetry run refine-alignment` / `retry-alignment`. That node has two hard requirements
that a plain `npx n8n start` does not satisfy out of the box.

## Correct start command

```bash
NODES_EXCLUDE='[]' npx n8n start
```

Or, if using nvm and Node isn't already on a supported version:

```bash
nvm use 24        # or: nvm alias default 24
NODES_EXCLUDE='[]' npx n8n start
```

Then open `http://localhost:5678`, import both workflow JSON files, and re-point the
orchestrator's "Run Chapter" Execute Workflow nodes at your imported copy of
`n8n-refine-retry-chapter.json` (they start as placeholders).

## Why both parts are required

1. **Node.js >= 24.** n8n 2.x's `package.json` declares `"engines": { "node": ">=24.0.0" }`.
   npm doesn't enforce `engines` by default, so `npx n8n start` on an older Node (e.g.
   v19) installs and boots without complaint, but produces subtle runtime issues.
   Check what's active with `node --version` / `nvm current`.

2. **`NODES_EXCLUDE='[]'`.** n8n 2.x ships with `n8n-nodes-base.executeCommand` and
   `n8n-nodes-base.localFileTrigger` **disabled by default for security reasons** (a
   2.0 breaking change — see `@n8n/config`'s `NodesConfig.exclude` default and
   `modules/breaking-changes/rules/v2/disabled-nodes.rule.js` in the installed
   package). This is a hard-coded default node blocklist, independent of the Node.js
   version fix above — without this override, Execute Command nodes will never load,
   no matter how new your Node.js is or how many times you refresh the browser.

   This is a deliberate opt-in: only set it because these workflows run
   self-authored, trusted commands (`poetry run refine-alignment` / `retry-alignment`)
   on your own machine — Execute Command is arbitrary shell execution from a workflow,
   which is exactly why n8n disables it by default.

## Symptom if you skip either fix

Opening the "Refine Alignment" / "Retry Alignment" node shows:

> Install this node to use it
> This node is not currently installed. It is either from a newer version of n8n, a
> custom node, or has an invalid structure.

and the canvas renders those nodes with a plain "?" icon. The terminal running n8n
prints, once per affected node instance:

```
Unrecognized node type: n8n-nodes-base.executeCommand
```

This is **not** a stale-browser-cache issue and a hard refresh alone will not fix it
(a hard refresh only helps if the backend was already started correctly and the tab
just hadn't re-fetched the node-types list yet).

## Other setup notes

- `Execute Command is disabled on n8n Cloud entirely` — these workflows require
  self-hosted n8n regardless of the `NODES_EXCLUDE` setting above.
- `workDir` in the workflow's "Configure Run" / trigger inputs must be the real repo
  root (e.g. `/home/paulhjwu/text-align`) so `cd {{workDir}} && poetry run ...` picks
  up `.env` via `load_dotenv()`.
- The host running n8n needs `poetry install` already done (torch/sentence-transformers
  resolved). Sanity check directly on that host: `poetry run refine-alignment --help`.
- Raise n8n's `EXECUTIONS_TIMEOUT` before running more than a couple of chapters —
  async batch-mode passes can take minutes to ~1 hour each.
- No overlap protection: don't trigger two runs against the same config+corpus+
  overlapping chapters concurrently.
