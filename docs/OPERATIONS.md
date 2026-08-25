# Operational guide

1. Probe reachability and persist the full runtime manifest when a graph will be
   materialized.
2. Compose and clean one active graph in ComfyUI.
3. Export its paired UI and API forms through Workspace Control.
4. Declare guarded parameter pointers and materialize a review-gated draft.
5. Validate the API graph against the same captured `/object_info`.
6. Resolve every missing custom-node type before submission.
7. Submit once and retain the exact prompt id.
8. Poll its history rather than trusting an ambiguous browser progress display.
9. Enumerate artifacts only from that history record.
10. Record the immutable run receipt with the exact semantic operation id,
   version, and contract hash.
11. Review outputs and separately assign `visually-accepted` or `rejected`.

## Paired materialization procedure

Use a Workspace Control export captured from the final active graph, not a UI
workflow and API prompt saved at different times. Save the full probe result:

```bash
comfy-runtime --url URL probe --output runtime-manifest.json
```

Prepare an opaque semantic operation reference and a guarded parameterization:

```json
{
  "id": "generate.keyframed",
  "version": 1,
  "contract_hash": "<64 lowercase hex characters>"
}
```

```json
{
  "schema": "comfy.api-parameterization/1",
  "parameters": [
    {
      "name": "first_frame_filename",
      "pointers": ["/1/inputs/image"],
      "expected": "captured-first.png"
    }
  ]
}
```

Then run:

```bash
comfy-runtime materialize-export workspace-export.json parameterization.json \
  --operation-ref operation-ref.json \
  --variant first-last \
  --runtime-manifest runtime-manifest.json \
  --output-dir drafts
```

Review the emitted UI graph and API template together. The manifest state
`schema-validated-draft` means the captured graph matched the captured schemas
and round-tripped exactly. It does not mean the graph executed, achieved its
visual objective, or is ready for automatic promotion.

Manager updates and process restarts are distinct operations. Apply a targeted
update only while the queue is idle, then restart the ComfyUI Python process if
the changed package contains Python. A short gateway error during restart is
expected; a persistent error requires the smallest external operator check.

## First install from an unknown Git URL

Manager's dedicated `POST /customnode/install/git_url` route is synchronous.
On Windows it waits for its clone/install subprocess inside the ComfyUI request,
so the UI websocket and HTTP origin can become temporarily unresponsive even
though the Cloudflare service itself has not been changed.

Use this protocol:

1. Verify both the Comfy queue and Manager queue are idle.
2. Submit one exact repository URL once.
3. Treat a client timeout as an unknown outcome, not as permission to resubmit.
4. Wait for the origin to return, then inspect `/customnode/installed` and the
   package capability route before deciding whether anything failed.
5. If the gateway remains unavailable, ask the external operator only to check
   the ComfyUI process and the tunnel service. Do not change CUDA, PyTorch,
   drivers, models, ComfyUI core, or unrelated node packs.
6. Once the package exists, use Manager's targeted update queue for later
   revisions; do not repeat the first-install route.
