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

## Materialization path

```text
active ComfyUI browser graph
  -> Workspace Control paired export (UI graph + API graph + hashes)
  -> guarded literal parameterization
  -> exact template round trip
  -> captured /object_info validation
  -> review-gated UI/API draft pair
```

Runtime Control does not synthesize a UI graph from an API prompt. The browser
export is the source of UI structure, and the paired API graph is the source of
execution structure. Parameterization is restricted to existing input literals;
links cannot be replaced by bindings. This preserves topology while separating
per-run values from the canonical graph.

A persisted full runtime manifest includes `_captured_object_info`. Its public
fields and schema snapshot are independently content-addressed. Compact public
manifests and run receipts omit the large snapshot and therefore cannot be used
as materialization schema evidence.
