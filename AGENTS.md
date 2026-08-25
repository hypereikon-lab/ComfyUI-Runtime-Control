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
- A completed queue item means `executes`, not visual success.
- Persist one immutable run receipt per submitted graph.
- Preserve dirty worktrees and never force-push.

## Verification

```bash
python3 -m compileall -q src
PYTHONPATH=src python3 -m unittest discover -s tests -v
git diff --check
```

Live checks must begin with `probe`. Do not submit a graph unless its API form
has been validated against the same runtime manifest.
