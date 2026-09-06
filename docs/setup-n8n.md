# Setting up a local n8n server for refine-alignment

This walks through setting up self-hosted n8n from scratch on this machine and
importing `n8n-refine-alignment-orchestrator.json` +
`n8n-refine-retry-chapter.json` so the orchestrator's "Run Chapter (...)" nodes
resolve automatically, without manual re-pointing. For the bare start command once
this is done once, see `docs/start-n8n.md`.

## 1. Prerequisites

- **Node.js >= 24.** n8n 2.x declares `"engines": { "node": ">=24.0.0" }`. npm doesn't
  enforce this, so an older Node (e.g. a leftover v19/v18 default) will still install
  and boot n8n, but causes hard-to-diagnose failures later (see "Why" in
  `docs/start-n8n.md`). Check and switch if needed:
  ```bash
  node --version
  nvm install 24 && nvm use 24     # if using nvm
  ```
- **Poetry deps installed** in this repo (`poetry install`), since the workflows shell
  out to `poetry run refine-alignment` / `retry-alignment`. Sanity check:
  ```bash
  poetry run refine-alignment --help
  ```
- API keys available to the n8n process's environment: this repo's root `.env`
  (auto-loaded by `load_dotenv()`), not n8n's own Credentials store — Execute Command
  subprocesses don't see n8n Credentials.

## 2. Start n8n with Execute Command enabled

```bash
NODES_EXCLUDE='[]' npx n8n start
```

n8n 2.x disables `n8n-nodes-base.executeCommand` (and `localFileTrigger`) **by
default for security** — running shell commands from a workflow is real risk, so only
do this because these two workflows only ever run trusted, self-authored commands
(`poetry run refine-alignment`/`retry-alignment`) on your own machine. Without this
flag, the node fails to load and its panel shows "Install this node to use it" no
matter how many times you refresh the browser or how new your Node.js is — see
`docs/start-n8n.md` for the full diagnosis.

On first run, n8n opens `http://localhost:5678` and asks you to create the local owner
account (email/password stored only in your local `~/.n8n/database.sqlite`).

## 3. Import both workflows so they auto-link

Both `docs/n8n-refine-alignment-orchestrator.json` and
`docs/n8n-refine-retry-chapter.json` now carry a fixed top-level `"id"` field
(`ta-refine-orchestrator` and `ta-refine-retry-chapter` respectively), and the
orchestrator's two "Run Chapter (...)" Execute Workflow nodes reference
`ta-refine-retry-chapter` directly (`mode: "id"`) instead of an empty placeholder.
This only resolves correctly if the retry-chapter workflow is actually **stored under
that same id** in your n8n instance — which import method you use decides whether that
happens automatically.

### Recommended: import via CLI (guaranteed to honor the fixed id)

```bash
# n8n must not be running against the same DB file at the same time as a CLI import;
# stop the `npx n8n start` process first, then (strictly in the following order):
npx n8n import:workflow --input=docs/n8n-refine-retry-chapter.json
npx n8n import:workflow --input=docs/n8n-refine-alignment-orchestrator.json
npx n8n start
```

`import:workflow` upserts on the JSON's own `id` (confirmed in
`n8n/dist/services/import.service.js`: `tx.upsert(WorkflowEntity, workflow, ['id'])`),
so it's also safe to re-run after editing either file — it updates the existing row in
place instead of creating a duplicate. This is the only path that's *guaranteed* to
land the retry-chapter workflow at `ta-refine-retry-chapter`, which is what makes the
orchestrator's Execute Workflow nodes resolve with zero manual steps.

### Alternative: import via the editor UI ("Import from File")

n8n's `POST /workflows` endpoint also honors a client-supplied `id` (checked in
`n8n/dist/workflows/workflows.controller.js`'s `create()` — it looks up `body.id` and
only errors if that id is already taken), so a fresh "Import from File" *should* also
land at the fixed id. This wasn't independently verified against the editor's
minified frontend bundle, though, so after importing each file this way:

1. Check the resulting workflow's URL. If it's
   `http://localhost:5678/workflow/ta-refine-retry-chapter` (and
   `.../ta-refine-orchestrator` for the other), the fixed id was honored — the
   orchestrator's nodes will resolve with no further steps.
2. If instead you see an auto-generated id (e.g. `xwqa4gcHhudG63Vk`), the UI import
   didn't preserve it. Either delete that workflow and re-import via the CLI method
   above, or manually repoint each "Run Chapter (...)" node in the orchestrator: open
   it, switch the Workflow field to "From list", and pick your imported copy of
   `n8n-refine-retry-chapter`.
3. **Re-importing the same file a second time via the UI will fail** with `Workflow
   with id ta-refine-retry-chapter exists already` rather than updating it (unlike the
   CLI's upsert) — for repeat imports after editing the JSON, use the CLI method, or
   open the existing workflow and use "Import from File" *from within it* to replace
   its contents in place.

### If you already have duplicate workflows from earlier imports

Before either of the JSON files had a fixed `id`, every "Import from File" created a
new row with a random id — if you already clicked import more than once, you likely
have duplicate `n8n-refine-alignment-orchestrator` / `n8n-refine-retry-chapter`
entries in your workflow list from before this fix. Open each list at
`http://localhost:5678/home/workflows`, and delete the stale/duplicate copies, keeping
(or replacing all of them with) the ones imported via the steps above.

## 4. After importing

- Open the orchestrator, open one of the "Run Chapter (...)" nodes, and confirm the
  Workflow field shows `n8n-refine-retry-chapter` resolved (not a placeholder / not
  "?"). If it shows correctly, no manual repointing is needed.
- Raise `EXECUTIONS_TIMEOUT` before running more than a couple of chapters —
  async batch-mode passes can take minutes to ~1 hour each.
- Don't trigger two runs against the same config+corpus+overlapping chapters
  concurrently — there's no overlap protection.

See the sticky notes inside each workflow (and `docs/start-n8n.md`) for the rest of
the operational details (workDir, Docker bind-mounts, do/while retry loop semantics,
exit-code contract).
