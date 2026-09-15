---@class FeedbackAuthority
---@field host string
---@field path_authority string

---@class FeedbackConnectionKey
---@field authority FeedbackAuthority
---@field socket string

---@class FeedbackWorktreeKey
---@field authority FeedbackAuthority
---@field canonical_root string

---@class FeedbackReviewKey
---@field worktree FeedbackWorktreeKey
---@field review_id string

---@class FeedbackWorkspaceSessionKey
---@field connection FeedbackConnectionKey
---@field workspace_id string
---@field scope 'workspace'

---@class FeedbackTabSessionKey
---@field connection FeedbackConnectionKey
---@field workspace_id string
---@field scope 'tab'
---@field tab_id string

---@alias FeedbackSessionKey FeedbackWorkspaceSessionKey|FeedbackTabSessionKey

---@class FeedbackRange
---@field version 1
---@field kind 'complete_lines'
---@field start_line integer
---@field end_line integer

---@class FeedbackSourceLocation
---@field worktree FeedbackWorktreeKey
---@field relative_path string
---@field kind 'working_tree'|'index'|'revision'|'snapshot'
---@field revision? string
---@field blob? string

---@class FeedbackSourceCapture
---@field location FeedbackSourceLocation
---@field range FeedbackRange
---@field lines string[]
---@field context_before string[]
---@field context_after string[]
---@field content_sha256 string

---@class FeedbackAnnotation
---@field schema_version 1
---@field id string
---@field revision integer
---@field review FeedbackReviewKey
---@field source FeedbackSourceCapture
---@field text string
---@field created_at string
---@field updated_at string
---@field deleted boolean

---@class FeedbackEditorIdentity
---@field token string
---@field handshake_verified boolean
---@field pid? integer
---@field start_identity? string

---@class FeedbackProcessIdentity
---@field pid integer
---@field start_identity string

---@class FeedbackCapabilities
---@field conditional_agent_delivery boolean
---@field cancel_mutation boolean

---@class FeedbackServerObservation
---@field connection FeedbackConnectionKey
---@field server_name string
---@field connected boolean
---@field process_identity? FeedbackProcessIdentity
---@field capabilities FeedbackCapabilities

---@alias FeedbackSessionState 'Absent'|'Starting'|'Visible'|'Hiding'|'Hidden'|'Showing'|'Moving'|'Reconciling'|'Closed'|'NeedsAttention'
---@alias FeedbackAction 'show'|'hide'|'focus'|'toggle'|'shutdown'
---@alias FeedbackDesiredAction 'show'|'hide'|'focus'|'shutdown'
---@alias FeedbackEffectKind 'create'|'move'|'focus'|'zoom'|'resize'|'shutdown'

---@class FeedbackDimensions
---@field columns integer
---@field rows integer

---@class FeedbackEffect
---@field kind FeedbackEffectKind
---@field from_tab? string
---@field to_tab? string
---@field pane_id? string

---@class FeedbackEditorView
---@field pane_id string
---@field host_tab_id string
---@field return_focus_pane_id? string
---@field placement 'split'|'zoomed'|'tab'|'overlay'
---@field content_dimensions FeedbackDimensions
---@field owned_effects FeedbackEffect[]

---@class FeedbackSessionOperation
---@field invocation_id string
---@field requested_action FeedbackAction
---@field desired_action FeedbackDesiredAction
---@field phase FeedbackSessionState
---@field unresolved_effect? FeedbackEffect
---@field guarded_tabs string[]
---@field outcome 'pending'|'confirmed'|'failed'|'uncertain'

---@class FeedbackEditorSession
---@field controller_protocol_version 1
---@field session_id string
---@field session_key FeedbackSessionKey
---@field owner_token string
---@field editor_identity FeedbackEditorIdentity
---@field config_generation integer
---@field observed_state FeedbackSessionState
---@field view? FeedbackEditorView
---@field operation? FeedbackSessionOperation

---@class FeedbackOriginContext
---@field window integer
---@field buffer integer
---@field cursor integer[]
---@field mode string
---@field visual_start? integer[]
---@field visual_end? integer[]
---@field invocation_id string
---@field composer_generation integer

---@class FeedbackBatchMember
---@field id string
---@field revision integer

---@class FeedbackTarget
---@field connection FeedbackConnectionKey
---@field workspace_id string
---@field tab_id string
---@field pane_id string
---@field agent_session_id string
---@field worktree FeedbackWorktreeKey

---@class FeedbackDeliveryBatch
---@field api_version 1
---@field id string
---@field review FeedbackReviewKey
---@field members FeedbackBatchMember[]
---@field payload string
---@field target? FeedbackTarget
---@field submit boolean
---@field strict_session_guard boolean

---@class FeedbackReceipt
---@field schema_version 1
---@field id string
---@field batch_id string
---@field review FeedbackReviewKey
---@field acknowledged FeedbackBatchMember[]
---@field outcome 'delivered_to_input'
---@field recorded_at string

---@class FeedbackError
---@field code string
---@field message string
---@field context table
---@field local_text? string

---@class FeedbackSuccess
---@field ok true
---@field value any

---@class FeedbackFailure
---@field ok false
---@field error FeedbackError

---@alias FeedbackResult FeedbackSuccess|FeedbackFailure
---@alias FeedbackDone fun(result: FeedbackResult)

---@class FeedbackOperation
---@field cancel fun()

---@class FeedbackSourceAdapter
---@field resolve fun(request: table, done: FeedbackDone): FeedbackOperation
---@field capture fun(request: table, done: FeedbackDone): FeedbackOperation
---@field navigate fun(request: table, done: FeedbackDone): FeedbackOperation

---@class FeedbackTransport
---@field list_targets fun(request: table, done: FeedbackDone): FeedbackOperation
---@field validate_target fun(request: table, done: FeedbackDone): FeedbackOperation
---@field deliver fun(request: table, done: FeedbackDone): FeedbackOperation

return {}
