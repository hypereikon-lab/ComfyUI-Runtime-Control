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

## Typical run

```bash
comfy-runtime --url https://comfy.example.invalid probe
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
