"""PR01 fixture-only public-operation lanes.

The native runner owns GUI/session guards; this module is intentionally only the
operation matrix consumed by the later fixture adapter.  It never invokes Herdr.
"""
LANES = {
    "root-switch": "delayed A send preserves B",
    "composer-root": "composer opened in A saves to A after visiting B",
    "new-comment": "new A draft survives pending acknowledgement",
    "edited-comment": "edited captured draft survives acknowledgement",
    "cancel-send": "picker cancellation sends no bytes and retains drafts",
    "send-failure": "failed fake input retains drafts with an error",
    "uncertain": "accepted bytes without acknowledgement retains drafts",
    "closed-origin": "closed composer origin does not focus another window",
    "restart": "legacy records reload with stable UUID identities",
    "batch": "only exact selected ID/revision acknowledgements clear",
}
