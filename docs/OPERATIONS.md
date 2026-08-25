# Operational guide

1. Probe reachability and capture the runtime manifest.
2. Export the desired ComfyUI graph in API format.
3. Validate that graph against the captured `/object_info`.
4. Resolve every missing custom-node type before submission.
5. Submit once and retain the exact prompt id.
6. Poll its history rather than trusting an ambiguous browser progress display.
7. Enumerate artifacts only from that history record.
8. Record the immutable run receipt with the exact semantic operation id,
   version, and contract hash.
9. Review outputs and separately assign `visually-accepted` or `rejected`.

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
