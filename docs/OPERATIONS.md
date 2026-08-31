# Operational guide

1. Probe reachability and persist the full runtime manifest when a graph will be
   materialized.
2. Evaluate the project's runtime-requirements profile against that same
   manifest and stop on any automated gate failure.
3. Complete declared manual checks such as free storage and local auto-start.
4. Compose and clean one active graph in ComfyUI.
5. Export its paired UI and API forms through Workspace Control.
6. Declare guarded parameter pointers and materialize a review-gated draft.
7. Validate the API graph against the same captured `/object_info`.
8. Resolve every missing custom-node type before submission.
9. Submit once and retain the exact prompt id.
10. Poll its history rather than trusting an ambiguous browser progress display.
11. Enumerate artifacts only from that history record.
12. Record the immutable run receipt with the exact semantic operation id,
   version, and contract hash.
13. Review outputs and separately assign `visually-accepted` or `rejected`.

The full probe keeps content-addressed snapshots of `/object_info`, `/models`,
and `/queue` beside compact endpoint hashes. `public_manifest()` strips all
large `_captured_*` values from receipts. A readiness report therefore remains
auditable without putting model inventories or full schemas into every run.
On current ComfyUI, `/models` lists model categories; exact filenames are
checked against the captured loader widgets in `/object_info`.

If direct HTTP access is unavailable because Cloudflare authentication exists
only in the browser, use that page to fetch the same bounded endpoints and pass
their JSON values to `manifest-from-snapshots`. Never extract the browser
cookie, paste it into a shell, or mark an endpoint available when it was not
captured. Omitted endpoints remain explicit failed requirements.

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

## Updating an already installed public repository

Use Repository Control instead of Manager when all of the following are true:

- the exact custom-node repository is already installed;
- its configured remote is the expected public GitHub repository;
- its worktree is clean;
- the desired revision is a known commit reachable by fast-forward from the
  current revision; and
- both the Comfy execution queue and Manager queue are idle.

The bounded sequence exposed by Repository Control 1.0.x is:

```text
GET /repository-control/v1/repos
  -> identify one exact repository
  -> verify clean worktree, public remote, branch, and current SHA
  -> publish and verify the exact target SHA externally
  -> POST /repository-control/v1/fast-forward with repo, branch,
     expected_head, expected_target, and expected_remote
  -> re-read inventory and verify observed SHA
  -> POST /manager/reboot only if imported Python changed
  -> verify representative node schemas after the origin returns
```

The request is a single guarded mutation, not a separately persisted plan-id
protocol. Its exact expected fields provide the compare-and-swap boundary; the
response retains before/after repository state.

Do not use this route to discover whether a private repository can authenticate,
to install a missing package, to repair a dirty worktree, to switch branches,
to force-reset history, or to run arbitrary Git commands. Those states must
fail closed. A Repository Control update and a Comfy process restart remain two
separate authorized operations.

Repository Control may update its own clean public clone by the same contract.
Its newly written Python code becomes active only after the subsequent Comfy
process restart, so the before/after receipt must retain both the on-disk SHA
and the post-restart imported capability evidence.

## First install from an unknown Git URL

Manager's dedicated `POST /customnode/install/git_url` route is synchronous.
On Windows it waits for its clone/install subprocess inside the ComfyUI request,
so the UI websocket and HTTP origin can become temporarily unresponsive even
though the Cloudflare service itself has not been changed.

### Hard preflight rule

Use this remote route only for a repository whose public visibility and default
branch have been verified before the request. A successful authenticated lookup
from the controlling workstation is not sufficient evidence: the Windows
account running ComfyUI may have no Git credentials. Do not embed a token in the
URL, copy browser credentials, or use the production origin to discover whether
a private clone happens to work.

Private repositories require an explicitly provisioned, non-interactive clone
mechanism on the host and a recovery path that does not depend on the ComfyUI
HTTP process. That mechanism is outside Runtime Control's Manager adapter.

Use this protocol:

1. Verify both the Comfy queue and Manager queue are idle.
2. Verify the repository is public and its default branch is the intended
   install target.
3. Confirm that an external operator or process supervisor can restart only
   ComfyUI if the origin blocks. A reboot endpoint on the same origin is not an
   independent recovery channel.
4. Run `plan-git-install` and preserve its public-visibility, branch, and
   independent-recovery assertions.
5. Submit one exact repository URL once with a new `--journal` path. Runtime
   Control writes the intent before making the HTTP request.
6. Treat a client timeout as an `outcome-unknown`, not as permission to
   resubmit. Never reuse or delete that journal to bypass the guard.
7. Wait for the origin to return, then run `reconcile-install`. It inspects
   `/customnode/installed` but never retries the install.
8. If the package is not listed, inspect the exact target directory for a
   partial clone before any retry. The journal deliberately records that this
   manual check is still required.
9. Inspect the package capability route before deciding whether anything
   failed.
10. If the gateway remains unavailable, ask the external operator only to check
   the ComfyUI process and the tunnel service. Do not change CUDA, PyTorch,
   drivers, models, ComfyUI core, or unrelated node packs.
11. Once the package exists, use Manager's targeted update queue for later
    revisions, or Repository Control when the installed clone satisfies the
    clean-public-fast-forward contract above; do not repeat the first-install
    route.

Example:

```bash
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
```

The successful terminal state is `reconciled-installed`. A
`reconciled-not-listed` result is not permission to retry: it means the
host-side partial-directory check remains unresolved.

For a non-Registry Git package, Manager's installed inventory may report the
current Git commit in `ver`. That SHA is deployment evidence, not a Registry
package version. A targeted update must still use `--version unknown` together
with the exact public `--source-url`; passing the observed SHA as `--version`
can enqueue a successful no-op. Runtime Control rejects that ambiguous
combination.

### Empirical incident: private clone blocked the origin

On 2026-08-26, the Windows lab runtime received a first-install request for a
GitHub repository that was still private. Manager entered its Windows clone
helper and Git waited for interactive credentials. The observable state was:

- the previously loaded ComfyUI page remained visible but stale;
- new HTTP requests, including `/system_stats`, did not complete;
- Cloudflare returned `524`, while `cloudflared` itself remained connected;
- a request to `/manager/reboot` could not recover the process because it
  depended on the same blocked ComfyUI origin;
- changing the repository to public after the clone had started did not release
  the already-running Git process.

The relevant Manager path awaits `core.gitclone_install()`. On Windows that
path invokes the Git helper through `subprocess.check_call()` without an
operation timeout. Therefore an interactive credential wait must be treated as
potentially unbounded, not as a slow clone that is guaranteed to time out.

Recovery is limited and non-destructive:

1. On the host, cancel any GitHub or Git Credential Manager prompt.
2. If the origin does not resume, stop and relaunch only the ComfyUI process.
   Do not restart or reconfigure `cloudflared` unless it independently failed.
3. After recovery, inspect the exact target directory and Manager inventory.
   A failed clone may leave a partial directory. Remove it only after verifying
   that it is the incomplete target and with the required deletion approval.
4. Re-run the install once, only after public visibility has been verified.

This incident is a control-plane failure, not evidence of damaged models,
drivers, CUDA, the GPU, the tunnel configuration, or other custom-node packs.
