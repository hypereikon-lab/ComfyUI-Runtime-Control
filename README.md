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
| R6 | Targeted Administration | plans and applies one exact Manager custom-node update; journals first installs and reconciles unknown outcomes; guarded Comfy process restart |
| R7 | Workspace Control | capability probe for the separate browser extension; no tab logic lives here |
| R8 | Run Registry | immutable receipts binding a semantic operation to graph, runtime, history, and artifacts |
| R9 | Paired Materialization | verifies one Workspace Control UI/API export, replaces guarded literals with bindings, and emits a review-gated draft pair |
| R10 | Durable Series | validates an explicit ordered graph plan once, persists every submitted prompt id, resumes without duplicate submission, and writes one receipt per step |
| R11 | Runtime Requirements | compares a project-supplied compatibility profile with one content-addressed live manifest without mutating the runtime |
| R12 | Durable Batch | validates independent prebound experiment graphs once, executes them with bounded serial GPU load, survives transport loss by exact prompt id, and records isolated terminal failures without inventing data dependencies |
| R13 | Availability Guard | samples global free RAM/VRAM and the Comfy queue, persists a continuous-observation window, and reports one transition when a shared host becomes ready or unavailable |

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

On macOS, the service token can remain in Keychain rather than a browser or a
plaintext shell profile. Runtime Control still reads only its process
environment; retrieve the two values immediately before the command:

```bash
cf_runtime_id=$(security find-generic-password -a "$USER" \
  -s 'hypereikon.comfy.cf-access-client-id' -w) || exit 21
cf_runtime_secret=$(security find-generic-password -a "$USER" \
  -s 'hypereikon.comfy.cf-access-client-secret' -w) || exit 22
cf_runtime_id=${cf_runtime_id#CF-Access-Client-Id: }
cf_runtime_secret=${cf_runtime_secret#CF-Access-Client-Secret: }

CF_ACCESS_CLIENT_ID="$cf_runtime_id" \
CF_ACCESS_CLIENT_SECRET="$cf_runtime_secret" \
comfy-runtime --url https://comfy.example.invalid probe
```

The variables exist only in that shell process and neither their values nor the
Cloudflare headers are printed, persisted in receipts, or copied from browser
state.

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

For a shared GPU host, total hardware and an idle Comfy queue are insufficient:
another application can occupy the GPU without appearing in `/queue`.
`observe-availability` advances a durable stability window from read-only
`/system_stats` and `/queue` samples:

```bash
comfy-runtime --url https://comfy.example.invalid observe-availability \
  examples/availability-policy.json \
  --state "$HOME/.local/state/comfy-runtime/shared-5090.json"
```

The example requires 24 GiB free VRAM, 16 GiB available RAM, and an idle queue
for 15 observed minutes. Samples farther apart than ten minutes break
continuity. The report distinguishes global free GPU memory from the current
Comfy PyTorch allocator and emits `became-ready` only once per availability
window. This command never submits, interrupts, unloads, or launches work.

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

Existing job outputs can be enumerated and downloaded without reopening their
workflow in the frontend:

```bash
comfy-runtime --url https://comfy.example.invalid recent-artifacts --limit 20
comfy-runtime --url https://comfy.example.invalid recent-artifacts --limit 100 \
  --filename exact-output.mp4
comfy-runtime --url https://comfy.example.invalid artifacts PROMPT_ID
comfy-runtime --url https://comfy.example.invalid download-artifacts PROMPT_ID \
  --downloads "$PWD/downloads"
```

The recent-history lookup is explicitly bounded to at most 500 jobs and an
optional filename match is exact. Downloads are resolved only from one exact
history record, streamed into a temporary file, and atomically finalized
beneath the requested directory. The
default downloads only `output` artifacts and refuses to replace a local file;
use `--type all` or `--overwrite` only when that broader behavior is explicit.

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
from that same graph as `comfy.workspace-export/2` (and Runtime Control still
accepts version 1 captures). Runtime Control can turn
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

## Durable independent batches

`run-batch` is for parameter sweeps and comparison matrices. Its graphs share a
test design but do not consume one another's outputs:

```bash
comfy-runtime --url https://comfy.example.invalid run-batch batch.json \
  --state state/batch.json \
  --receipts receipts/batch \
  --downloads downloads/batch
```

```json
{
  "schema": "comfy.run-batch/1",
  "id": "control-strength-matrix",
  "steps": [
    {
      "id": "strength-025",
      "graph": "graphs/strength-025.api.json",
      "operation_ref": "operation-ref.json"
    },
    {
      "id": "strength-050",
      "graph": "graphs/strength-050.api.json",
      "operation_ref": "operation-ref.json"
    }
  ]
}
```

All graphs validate against one fresh runtime manifest before the first
submission. GPU work is serialized, but no false `depends_on` relation is
recorded. Every returned prompt id is saved atomically before polling. A tunnel
failure therefore leaves the step `submitted` for exact-id resumption. A
terminal Comfy execution failure gets a `blocked` receipt and does not suppress
the remaining independent tests. Batch-owned downloads contain only unique
`output` artifacts and are atomically replaceable during resume.

## Mutation boundary

```bash
comfy-runtime --url URL plan-update ComfyUI-Cauce
comfy-runtime --url URL apply-update ComfyUI-Cauce \
  --version unknown --source-url https://github.com/hypereikon-lab/ComfyUI-Cauce \
  --confirm ComfyUI-Cauce
comfy-runtime plan-git-install https://github.com/owner/public-node-pack \
  --visibility-confirmation public --default-branch main \
  --recovery-channel external-operator
comfy-runtime --url URL install-git https://github.com/owner/public-node-pack \
  --visibility-confirmation public --default-branch main \
  --recovery-channel external-operator \
  --journal state/install-public-node-pack.json \
  --confirm https://github.com/owner/public-node-pack
comfy-runtime --url URL reconcile-install \
  --journal state/install-public-node-pack.json
comfy-runtime --url URL restart-comfy --confirm restart-comfy-process
```

`install-git` persists its exact intent before contacting Manager. If the HTTP
request disappears, the journal becomes `outcome-unknown`; the command will not
reuse that path or authorize a retry. `reconcile-install` checks Manager's
installed inventory and still leaves any partial-directory inspection as an
explicit host-side action.

There is no update-all, arbitrary shell, filesystem browser, physical reboot,
model deletion, or GPU-stack mutation command.

For repositories that are already installed, the complementary
`ComfyUI-Repository-Control` extension is the preferred narrow update plane:
it inventories one exact clone, plans a known public clean fast-forward, and
applies only that plan. Manager remains responsible for first installation and
the separately authorized Comfy process restart.

See [Architecture](docs/ARCHITECTURE.md), [Operational guide](docs/OPERATIONS.md),
and [Remote ComfyUI runtime](docs/REMOTE_COMFY_RUNTIME.md).
