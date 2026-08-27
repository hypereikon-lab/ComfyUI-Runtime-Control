# ComfyUI Runtime Control runbook

This repository is a neutral HTTP client. It knows ComfyUI runtime contracts,
not MiniMax H3, CAUCE, a creative project, or browser layout.

## Invariants

- Treat `/object_info` as the live node-schema authority.
- Keep UI workflow JSON distinct from executable API prompt JSON.
- Never print, persist, or commit Cloudflare Access secrets.
- Default to read-only operations. Submission and mutation must be explicit.
- Manager changes require an exact custom-node identifier and a matching
  confirmation value. Never provide update-all, core update, model deletion,
  CUDA/PyTorch update, arbitrary shell, or physical reboot operations.
- The remote Manager Git-URL install path is for repositories verified public
  before submission. Never test private-repository access through the live
  ComfyUI origin: on Windows the synchronous Git subprocess can wait for an
  interactive credential prompt without a timeout and make the origin unable
  to serve even its own reboot route. Private installs require a separately
  provisioned non-interactive credential path and an external recovery channel.
- Persist a new exact install journal before every first-install request. A
  timeout or transport loss is `outcome-unknown`: reconcile the existing
  journal against Manager inventory and require a host-side partial-directory
  check when absent. Never auto-retry or overwrite an install journal.
- A completed queue item means `executes`, not visual success.
- Persist one immutable run receipt per submitted graph.
- Materialize reusable graph drafts only from one Workspace Control export that
  contains paired UI/API graphs and verified hashes. Parameterize existing
  literal inputs with expected-value guards; never replace graph links.
- A full runtime manifest persisted by `probe --output` may support schema
  validation. A compact public manifest cannot because it omits
  `_captured_object_info`.
- Materialized output remains review-gated. Never install, queue, or promote it
  automatically.
- Require a semantic operation id, version, and contract hash for every run
  receipt. Treat that reference as opaque provenance; do not interpret CAUCE,
  H3, or project semantics here.
- Preserve dirty worktrees and never force-push.
- Durable series are strictly serial. Persist a submitted prompt id before
  polling it, resume that exact id, and require completed steps to form a
  content-addressed prefix. Never infer H3 continuity or pass artifacts between
  steps implicitly; each executable graph must already contain exact inputs.
- Runtime requirements are opaque project policy. Evaluate exact endpoints,
  node types, model filenames, hardware thresholds, and queue state against one
  full captured manifest; do not reinterpret or silently relax failed gates.
- Free storage and physical power/recovery are manual checks unless a bounded,
  authoritative runtime route is available. Never infer them from GPU memory or
  model lists.

## Verification

```bash
python3 -m compileall -q src
PYTHONPATH=src python3 -m unittest discover -s tests -v
git diff --check
```

Live checks must begin with `probe`. Do not submit a graph unless its API form
has been validated against the same runtime manifest.

The laboratory tunnel, frontend, Manager, browser-control, deployment, and
recovery model is documented in `docs/REMOTE_COMFY_RUNTIME.md`. Keep that
runtime knowledge here rather than in creative custom-node packages.
