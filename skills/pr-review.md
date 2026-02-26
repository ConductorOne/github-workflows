---
name: pr-review
description: Review a baton connector PR in CI. Performs a structured, read-only code review using focused sub-agents.
---

# Review Baton Connector PR (CI)

Perform a structured code review of a baton connector PR.

This skill runs in CI — do NOT write files, create commits, or run build/test commands.

**You MUST complete ALL of the following steps in order. Do NOT skip any step. Each step has a deliverable — produce it before moving on.**

| Step | Deliverable |
|------|-------------|
| 1. Determine Context | List of changed files by category |
| 2. Spawn Review Agents | All agent tasks launched (including docs-reviewer) |
| 3. Validate and Aggregate | Merged findings list |
| 4. Post Results | Summary comment with ALL required sections |

---

## Step 1: Determine Context

1. **Changed files:** Identify files changed in this PR from the diff context provided. Exclude `vendor/`, `conf.gen.go`, non-`.go` files (keep `go.mod`/`go.sum` and `docs/connector.mdx`). Stop if empty (no Go files and no docs changes).
2. **PR context:** Use the PR title, description, comments, and review comments provided in the conversation context.
3. **Classify files** into categories per the table below.

**Deliverable:** Print the list of changed files grouped by category before proceeding.

### File Classification

| File Pattern | Category | Agent |
|---|---|---|
| `pkg/connector/client*.go`, `pkg/client/*.go` | Client | sync-reviewer |
| `pkg/connector/connector.go` | Connector Core | sync-reviewer |
| `pkg/connector/resource_types.go` | Resource Types | sync-reviewer |
| `pkg/connector/<resource>.go` (users.go, groups.go, etc.) | Resource Builder | sync-reviewer |
| `pkg/connector/*_actions.go`, `pkg/connector/actions.go` | Provisioning | provisioning-reviewer |
| `pkg/config/config.go` | Config | lightweight-reviewer |
| `go.mod`, `go.sum` | Dependencies | lightweight-reviewer |
| `docs/connector.mdx` | Documentation | docs-reviewer |

---

## Step 2: Spawn Review Agents

For each category of files, read the relevant diffs from the PR context provided. If you need full file content to evaluate a finding, use the Read tool.

Spawn agents in parallel using the Task tool: up to 3 code review agents (Agents 1-3) plus the docs-reviewer (Agent 4) which always runs.

### Agent Spawning Rules

- If no provisioning files changed → skip provisioning-reviewer
- If no config/dep files changed → skip lightweight-reviewer
- If only config/dep files changed → skip sync-reviewer, only spawn lightweight-reviewer
- **Always spawn docs-reviewer** (Agent 4) — it runs on every PR
- Always spawn at least one code review agent

### Agent 1: sync-reviewer

Spawn with `subagent_type: "general-purpose"`. Reviews ALL non-provisioning Go files including breaking change detection.

**Prompt template:**

```
You are a code reviewer for a Baton connector (Go project syncing identity data from SaaS APIs into ConductorOne).

Review the diffs below against these criteria. Only flag issues you are confident are real problems — things you would reject in a human code review. Do not flag style preferences or hypothetical concerns.

For each finding provide JSON:
{"id": "<code>", "file": "<path>", "lines": "<range>", "description": "<issue>", "recommendation": "<fix>"}

Return a JSON array. Empty array if no issues.

## CLIENT CRITERIA (C1-C7)
- C1: API endpoints documented at top of client.go (endpoints, docs links, required scopes)
- C2: Must use uhttp.BaseHttpClient, not raw http.Client
- C3: Rate limits: return annotations with v2.RateLimitDescription from response headers
- C4: All list functions must paginate
- C5: DRY: central doRequest function; WithQueryParam patterns
- C6: URL construction via url.JoinPath or url.Parse, never string concat
- C7: Endpoint paths as constants, not inline strings

## RESOURCE CRITERIA (R1-R11)
- R1: List methods return []*Type (pointer slices)
- R2: No unused function parameters
- R3: Clear variable names (groupMember not gm)
- R4: Errors use %w (not %v), include baton-{service}: prefix, use uhttp.WrapErrors
- R5: Use StaticEntitlements for uniform entitlements
- R6: Use Skip annotations (SkipEntitlementsAndGrants, etc.) appropriately
- R7: Missing API permissions = degrade gracefully, don't fail sync
- R8: Pagination via SDK pagination.Bag (Push/Next/Marshal). Return "" when done. NEVER hardcode tokens. NEVER buffer all pages.
- R9: User resources include: status, email, profile, login
- R10: Resource IDs = stable immutable API IDs, never emails or mutable fields
- R11: All API calls receive ctx; long loops check ctx.Done()

## CONNECTOR CRITERIA (N1-N4)
- N1: ResourceSyncers() returns all implemented builders
- N2: Metadata() has accurate DisplayName/Description
- N3: Validate() exercises API credentials (not just return nil)
- N4: New() accepts config, creates client properly

## HTTP SAFETY (H1-H5)
- H1: defer resp.Body.Close() AFTER err check (panic if resp nil)
- H2: No resp.StatusCode/resp.Body access when resp might be nil
- H3: Type assertions use two-value form: x, ok := val.(Type)
- H4: No error swallowing (log.Println + continue = silent data loss)
- H5: No secrets in logs (apiKey, password, token values)

## BREAKING CHANGES (B1-B8) — Check in diffs:
- B1: Resource type Id: field changes (grants orphaned)
- B2: Entitlement slug changes in NewAssignmentEntitlement/NewPermissionEntitlement
- B3: Resource ID derivation changes (user.ID→user.Email)
- B4: Parent hierarchy changes (org→workspace)
- B5: Removed resource types/entitlements
- B6: Trait type changes (NewUserResource→NewAppResource)
- B7: New required OAuth scopes
- B8: SAFE (do not flag): display name changes, adding new types, adding trait options, adding pagination

## TOP BUG DETECTION PATTERNS
1. Pagination: `return resources, "", nil, nil` without conditional = stops after page 1
2. Pagination: `return resources, "next", nil, nil` hardcoded = infinite loop
3. HTTP: defer resp.Body.Close() BEFORE if err != nil = panic
4. HTTP: resp.StatusCode in error path without resp != nil check = panic
5. Type assertion: .(Type) without , ok := = panic
6. Error: log.Print(err) without return = silent data loss
7. Error: fmt.Errorf("...%v", err) should be %w
8. IDs: .Email as 3rd arg to NewUserResource = unstable ID
9. ParentResourceId.Resource without nil check = panic

Read the FULL file content (using Read tool) ONLY when the diff suggests a potential issue that requires full-file context (e.g., pagination flow, resource builder structure). For simple pattern issues visible in the diff, the diff alone is sufficient.

FILES AND DIFFS:
<paste diffs here, grouped by file>
```

### Agent 2: provisioning-reviewer

Only spawn if changed files contain `*_actions.go` or `actions.go` files. This agent MUST read the full provisioning files (not just diffs) because entity source correctness requires understanding the complete Grant/Revoke flow.

**Prompt template:**

```
You are reviewing provisioning (Grant/Revoke) code for a Baton connector.

Only flag issues you are confident are real problems — things you would reject in a human code review. Do not flag style preferences or hypothetical concerns.

CRITICAL CONTEXT — Entity Source Rules (caused 3 production reverts):
- WHO (user/account ID): principal.Id.Resource
- WHAT (group/role): entitlement.Resource.Id.Resource
- WHERE (workspace/org): principal.ParentResourceId.Resource
- NEVER get context from entitlement.Resource.ParentResourceId

In Revoke:
- Principal: grant.Principal.Id.Resource
- Entitlement: grant.Entitlement.Resource.Id.Resource
- Context: grant.Principal.ParentResourceId.Resource

Review criteria (P1-P6, H1-H5):
- P1: Entity source correctness per rules above
- P2: Revoke uses grant.Principal and grant.Entitlement correctly
- P3: Grant handles "already exists" as success; Revoke handles "not found" as success
- P4: Validate params before API calls; wrap errors with gRPC status codes
- P5: API argument order — multiple string params are easy to swap (verify against function signature)
- P6: ParentResourceId nil check before access
- H1-H5: (same HTTP safety rules as sync-reviewer)

Read the full provisioning files using the Read tool, then check the diff for what changed.

Return JSON array of findings (same format as sync-reviewer). Empty array if no issues.

FILES TO READ: <list full paths>
DIFFS: <paste diffs>
```

### Agent 3: lightweight-reviewer

Only spawn if changed files contain config or dependency files. Use `model: "haiku"` for efficiency.

**Prompt template:**

```
Review these connector config/dependency changes. Only flag issues you are confident are real problems.

Config criteria (G1-G4):
- G1: conf.gen.go must NEVER be manually edited
- G2: Fields use field.StringField/BoolField from SDK
- G3: Required fields: WithRequired(true); secrets: WithIsSecret(true)
- G4: No hardcoded credentials/URLs; base URL configurable

Dependency checks:
- Is baton-sdk at a recent version?
- Any unexpected new dependencies?
- Any removed deps still needed?

Return JSON array of findings (same format as sync-reviewer). Empty array if no issues.

DIFFS:
<paste diffs>
```

### Agent 4: docs-reviewer

**Always spawn this agent.** It checks whether the PR's code changes require updates to `docs/connector.mdx`. Spawn with `subagent_type: "general-purpose"`.

**Prompt template:**

```
You are checking whether a baton connector PR requires documentation updates.

The file docs/connector.mdx documents the connector's capabilities, configuration, and credentials for end users. Your job is to determine if the code changes in this PR make the docs stale.

Procedure:

1. Check if docs/connector.mdx exists in the repo (use the Glob tool).
2. If it does not exist, return: {"status": "no_docs"}
3. If it exists, check whether it is included in the PR's changed files list below.
4. If it is in the changed files, return: {"status": "docs_updated"}
5. If it exists but is NOT in the changed files, check the code diffs below for:

- D1: Capabilities table — Resource types added, removed, or changed sync/provision support (new resource builders, removed ResourceSyncers entries, added Grant/Revoke methods).
- D2: Connector actions — Action schemas added, removed, or modified (new BatonActionSchema definitions, changed action names, added/removed arguments).
- D3: Credential requirements — Required API scopes or permissions changed (new OAuth scopes, different permission levels, new authentication methods).
- D4: Configuration fields — Config fields added, removed, or renamed in pkg/config/config.go.

If any of D1-D4 apply, read docs/connector.mdx to confirm the specific section that would need updating. Documentation staleness is a blocker — the docs must be updated before merge.

Return a JSON object:
{"status": "stale", "findings": [{"id": "D1", "section": "<section name in docs>", "reason": "<why it's stale>"}]}

Or if none apply:
{"status": "up_to_date"}

CHANGED FILES:
<list of changed file paths>

DIFFS:
<paste diffs>
```

**Deliverable:** All agent tasks launched. Wait for them to complete before proceeding.

---

## Step 3: Validate and Aggregate

1. Parse JSON arrays from code review agents (Agents 1-3).
2. Deduplicate: same file + line range → keep one.
3. **Cross-validate entity sources** (if provisioning changed): Read the Grant/Revoke code yourself to verify P1/P2 findings. This is the #1 bug.
4. **Cross-validate PR feedback**: Check PR review comments against findings. Add any unaddressed items from human reviewers.
5. Drop findings that are fully mitigated by a config flag or feature gate.
6. Parse the docs-reviewer (Agent 4) result. If status is "stale", include each finding — stale docs are a release blocker.

**Deliverable:** A merged list of all findings with duplicates removed. Print the count.

---

## Step 4: Post Results

Post findings directly as PR comments:

1. **Inline comments** on specific lines where issues are found. Keep each comment to 2-3 sentences: what's wrong, why it matters, and how to fix it.

2. **Summary comment** — be concise. No more than a few sentences per finding. Use the following template (do not omit any section):

```
### PR Review: <PR title>

**Issues: N**

### Issues
<one-liner per issue with file:line, or "None found.">

### Documentation
<one-liner: "Up to date", "No docs file", or which sections need updating and why>
```

Do NOT include a "Files Reviewed" section, a "Verdict" section, or a "Breaking Changes" section. Do NOT repeat findings that were already posted as inline comments — just reference them briefly in the summary. Keep the entire summary comment short.
