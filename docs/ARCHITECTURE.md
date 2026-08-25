# Architecture

```text
operation reference / API template + exact bindings
          |
          v
deterministic compile -> live /object_info -> schema validation + dependency plan
          |                         |
          +-------------------------+
                                    v
                            exact /prompt job
                                    |
                         /history/{prompt_id}
                                    |
                         artifacts + run receipt
```

The client controls only the HTTP origin exposed by ComfyUI. A Cloudflare
Tunnel does not grant shell, desktop, arbitrary filesystem, or physical power
access. Browser workflow tabs are client-side state and belong to the separate
Workspace Control extension.

Runtime manifests and receipts use canonical JSON hashes. Secrets are request
headers only; they never enter manifests or receipts.
