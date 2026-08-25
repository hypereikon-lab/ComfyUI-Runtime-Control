# Operational guide

1. Probe reachability and capture the runtime manifest.
2. Export the desired ComfyUI graph in API format.
3. Validate that graph against the captured `/object_info`.
4. Resolve every missing custom-node type before submission.
5. Submit once and retain the exact prompt id.
6. Poll its history rather than trusting an ambiguous browser progress display.
7. Enumerate artifacts only from that history record.
8. Record the immutable run receipt.
9. Review outputs and separately assign `visually-accepted` or `rejected`.

Manager updates and process restarts are distinct operations. Apply a targeted
update only while the queue is idle, then restart the ComfyUI Python process if
the changed package contains Python. A short gateway error during restart is
expected; a persistent error requires the smallest external operator check.
