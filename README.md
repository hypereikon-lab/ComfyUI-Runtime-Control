# ComfyUI Runtime Control

A neutral, schema-aware client for operating a ComfyUI HTTP origin through a
local network or an authenticated reverse tunnel. It does not contain H3,
CAUCE, project, prompt, or browser-layout logic.

## Runtime operations

| ID | Operation | Implementation |
| --- | --- | --- |
| R1 | Runtime Probe | captures route availability, hashes, node schemas, features, and system stats |
| R2 | Workflow Compiler | resolves explicit template bindings and validates the result against live `/object_info` |
| R3 | Job Runner | submits `/prompt`, polls exact `/history/{id}`, interrupts or deletes exact jobs |
| R4 | Artifact Resolver | enumerates history outputs and retrieves authenticated `/view` artifacts |
| R5 | Dependency Planner | reports exact required, available, and missing node types |
| R6 | Targeted Administration | plans and applies one exact Manager custom-node update; guarded Comfy process restart |
| R7 | Workspace Control | capability probe for the separate browser extension; no tab logic lives here |
| R8 | Run Registry | immutable receipts binding a semantic operation to graph, runtime, history, and artifacts |
| R9 | Paired Materialization | verifies one Workspace Control UI/API export, replaces guarded literals with bindings, and emits a review-gated draft pair |
| R10 | Durable Series | validates an explicit ordered graph plan once, persists every submitted prompt id, resumes without duplicate submission, and writes one receipt per step |
| R11 | Runtime Requirements | compares a project-supplied compatibility profile with one content-addressed live manifest without mutating the runtime |

## Install

```bash
python3 -m pip install -e .
```

Cloudflare Access service credentials are optional and read only from the
process environment:

```text
CF_ACCESS_CLIENT_ID
CF_ACCESS_CLIENT_SECRET
```

Interactive Access cookies are intentionally not extracted from a browser.

`check-requirements` can operate live or completely offline on a previously
captured full manifest:

```bash
comfy-runtime --url https://comfy.example.invalid check-requirements \
  requirements.json --output runtime-readiness.json

comfy-runtime check-requirements requirements.json \
  --runtime-manifest runtime-manifest.json \
  --output runtime-readiness.json
```

The profile may require exact endpoints, node types, alternative node-type
groups, model filenames, minimum RAM/VRAM, an expected device-name fragment,
and an idle queue. Exact filenames are matched against live loader choices in
`/object_info`; the root `/models` route contributes model-category inventory,
not a recursive file listing. Manual checks such as free disk space remain explicit and
do not masquerade as automatically observed facts.

When Cloudflare provides only an interactive browser session, capture the
bounded JSON endpoints in that authenticated page and assemble the same
content-addressed manifest without exporting its cookie:

```bash
comfy-runtime manifest-from-snapshots \
  --runtime-label authenticated-browser-handoff \
  --snapshot features=captures/features.json \
  --snapshot system_stats=captures/system_stats.json \
  --snapshot object_info=captures/object_info.json \
  --snapshot models=captures/models.json \
  --snapshot queue=captures/queue.json \
  --output runtime-manifest.json
```

Only the fixed probe endpoint names are accepted. A snapshot manifest and a
direct HTTP probe use the same hashes, validation, and readiness evaluation.

## Typical run

```bash
comfy-runtime --url https://comfy.example.invalid probe
comfy-runtime --url https://comfy.example.invalid check-requirements requirements.json
comfy-runtime --url https://comfy.example.invalid compile graph.template.json bindings.json \
  --output graph.api.json
comfy-runtime --url https://comfy.example.invalid validate graph.api.json
comfy-runtime --url https://comfy.example.invalid run graph.api.json \
  --operation-ref operation-ref.json --receipts receipts --downloads downloads
```

`operation-ref.json` contains a semantic `id`, positive `version`, and SHA-256
`contract_hash`. Runtime Control treats it as neutral provenance; it does not
interpret CAUCE or project semantics.

`run` captures one runtime manifest, validates against that schema snapshot,
submits, waits for the exact prompt id, resolves artifacts, and records an
`executes` receipt. Visual acceptance is a later human evidence update, never
inferred from queue completion.

An API template uses an exact placeholder object wherever a runtime value is
required:

```json
{
  "1": {
    "class_type": "LoadImage",
    "inputs": { "image": { "$binding": "input_filename" } }
  }
}
```

Compilation fails for missing or unused bindings and for any result that does
not match current live node schemas. Optional branches use separate templates;
they are not left muted or bypassed in a shared graph.

## UI/API materialization bridge

Workspace Control exports the active browser graph and the API prompt generated
from that same graph as `comfy.workspace-export/1`. Runtime Control can turn
that captured pair into a reproducible, variant-scoped draft without inventing
node ids or reconstructing a browser workflow from conversational memory:

```bash
comfy-runtime --url https://comfy.example.invalid probe \
  --output runtime-manifest.json

comfy-runtime materialize-export workspace-export.json parameterization.json \
  --operation-ref operation-ref.json \
  --variant first-last \
  --runtime-manifest runtime-manifest.json \
  --output-dir drafts
```

`parameterization.json` names only captured literal inputs and guards each JSON
pointer with its expected value:

```json
{
  "schema": "comfy.api-parameterization/1",
  "parameters": [
    {
      "name": "prompt",
      "pointers": ["/12/inputs/prompt"],
      "expected": "captured prompt"
    }
  ]
}
```

The command verifies both export hashes, rejects graph-link substitution,
recompiles the resulting template with its captured bindings, and requires the
compiled API hash to equal the exported API graph hash. With a full probe
manifest it also validates against the captured `/object_info`. It writes a UI
graph, API template, bindings, and materialization manifest without overwriting
existing files by default.

The result remains `requires-live-review`. It is not automatically installed,
queued, or promoted into an operation repository.

## Durable serial execution

`run-series` executes already-materialized API graphs in one exact order. It is
for long production chains whose graph inputs have been bound explicitly; it
does not infer how one step's artifact should become the next step's input.

```bash
comfy-runtime --url https://comfy.example.invalid run-series series.json \
  --state state/series.json \
  --receipts receipts/series \
  --downloads downloads/series
```

The plan is deliberately small and neutral:

```json
{
  "schema": "comfy.run-series/1",
  "id": "forest-branch-a",
  "steps": [
    {
      "id": "segment-01",
      "graph": "graphs/segment-01.api.json",
      "operation_ref": "operations/segment-01.json",
      "depends_on": null
    },
    {
      "id": "segment-02",
      "graph": "graphs/segment-02.api.json",
      "operation_ref": "operations/segment-02.json",
      "depends_on": "segment-01"
    }
  ]
}
```

All paths are relative to the plan. Before any submission, every graph is
validated against the same fresh `/object_info` snapshot. After `/prompt`
returns, the exact prompt id is atomically persisted before polling begins. If
the process or tunnel disappears, rerunning the same command polls the stored
id; it does not submit a duplicate. A plan revision changes its hash and cannot
reuse stale state. Completion remains technical `executes` evidence, not visual
acceptance.

## Mutation boundary

```bash
comfy-runtime --url URL plan-update ComfyUI-Cauce
comfy-runtime --url URL apply-update ComfyUI-Cauce \
  --version unknown --source-url https://github.com/hypereikon-lab/ComfyUI-Cauce \
  --confirm ComfyUI-Cauce
comfy-runtime --url URL install-git https://github.com/owner/public-node-pack \
  --confirm https://github.com/owner/public-node-pack
comfy-runtime --url URL restart-comfy --confirm restart-comfy-process
```

There is no update-all, arbitrary shell, filesystem browser, physical reboot,
model deletion, or GPU-stack mutation command.

See [Architecture](docs/ARCHITECTURE.md) and [Operational guide](docs/OPERATIONS.md).
