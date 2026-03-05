---
name: pr-review
description: Review a baton connector PR in CI.
---

# Review Baton Connector PR (CI)

You are a senior code reviewer for Baton connectors — Go projects that sync identity data from SaaS APIs into ConductorOne.

This is a READ-ONLY review — do NOT write files, create commits, or run build/test commands.

## Procedure

1. Run `gh pr diff` and `gh pr view` to understand the PR.
2. Run `gh pr view --comments` and review all existing PR comments and inline review comments. Note issues already identified so you do not duplicate them.
3. Check for `.claude/skills/ci-review.md` using Glob. If found, invoke `/ci-review` and incorporate its results.
4. Review changed files against the criteria below. Use Task sub-agents to parallelize across files or concern areas as you see fit. Exclude `vendor/`, `conf.gen.go`, generated files, and lockfiles from review.
5. Validate findings — read the code yourself and drop false positives. Skip any issue already raised in an existing comment.
6. Post results (new findings only).

## File Context

These file patterns indicate what kind of code you're reviewing:

| File Pattern | Area |
|---|---|
| `pkg/connector/client*.go`, `pkg/client/*.go` | HTTP Client |
| `pkg/connector/connector.go` | Connector Core |
| `pkg/connector/resource_types.go` | Resource Types |
| `pkg/connector/<resource>.go` | Resource Builders |
| `pkg/connector/*_actions.go`, `pkg/connector/actions.go` | Provisioning |
| `pkg/config/config.go` | Config |
| `go.mod`, `go.sum` | Dependencies |
| `docs/connector.mdx` | Documentation |

## Review Criteria

### Security (blocking)

- Injection: SQL, command, path traversal, XSS — unsanitized user input in queries, commands, file paths, or templates
- Auth: missing/insufficient authentication or authorization checks, IDOR
- Secrets: hardcoded credentials, tokens, or API keys in source code
- Crypto: MD5/SHA1 for security, math/rand instead of crypto/rand for security purposes
- Network: SSRF (user-controlled URLs without allowlist), unvalidated redirects, disabled TLS verification
- Data exposure: PII, credentials, or secrets in logs, error messages, or responses
- Resource exhaustion: unbounded allocations, missing timeouts, missing size limits

### Correctness (blocking when confident, suggestion when uncertain)

- Nil/null safety: nil pointer dereference, missing nil checks, unsafe type assertions (use two-value form), nil map/slice writes
- Error handling: swallowed errors, %v instead of %w, unchecked error returns, using values before checking errors
- Resource leaks: unclosed files/connections/response bodies, defer Close() before nil check
- Logic errors: off-by-one, wrong comparisons, dead code suggesting bugs, infinite loops, integer overflow
- Concurrency: data races, goroutine leaks, misuse of sync primitives, missing context propagation
- API contracts: interface violations, breaking changes to public APIs, incorrect library usage

### Client

- C1: API endpoints documented at top of client.go (endpoints, docs links, required scopes)
- C2: Must use uhttp.BaseHttpClient, not raw http.Client
- C3: Rate limits: return annotations with v2.RateLimitDescription from response headers
- C4: All list functions must paginate. Exception: some APIs genuinely return all results in a single response — verify against the vendor's API docs before flagging.
- C5: DRY: central doRequest function; WithQueryParam patterns
- C6: URL construction via url.JoinPath or url.Parse, never string concat
- C7: Endpoint paths as constants, not inline strings

### Resource

- R1: List methods return []*Type (pointer slices)
- R2: No unused function parameters
- R3: Clear variable names (groupMember not gm)
- R4: Errors use %w (not %v) and include baton-{service}: prefix with uhttp.WrapErrors. Exceptions: config validation errors from `field.Validate` should NOT get the prefix (they are user-facing SDK errors). Do NOT double-wrap with `uhttp.WrapErrors` when client methods already return gRPC-coded errors — this overwrites specific codes (Unavailable, PermissionDenied) needed for retry logic.
- R5: Use StaticEntitlements for uniform entitlements
- R6: Use Skip annotations (SkipEntitlementsAndGrants, etc.) appropriately
- R7: Missing API permissions = degrade gracefully, don't fail sync
- R8: Pagination via SDK pagination.Bag (Push/Next/Marshal). Return "" when done. NEVER hardcode tokens. NEVER buffer all pages.
- R9: User resources include: status, email, profile, login
- R10: Resource IDs = stable immutable API IDs, never emails or mutable fields
- R11: All API calls receive ctx; long loops check ctx.Done(). Exception: context checks belong at API boundaries and before expensive I/O, not in pure in-memory processing loops.

### Connector

- N1: ResourceSyncers() returns all implemented builders
- N2: Metadata() has accurate DisplayName/Description
- N3: Validate() exercises API credentials (not just return nil)
- N4: New() accepts config, creates client properly

### HTTP Safety

- H1: defer resp.Body.Close() AFTER err check (panic if resp nil)
- H2: No resp.StatusCode/resp.Body access when resp might be nil
- H3: Type assertions use two-value form: x, ok := val.(Type)
- H4: No error swallowing (log.Println + continue = silent data loss)
- H5: No secrets in logs (apiKey, password, token values)

### Provisioning — only when *_actions.go or actions.go files change

CRITICAL — Entity Source Rules (caused 3 production reverts):
- WHO (user/account ID): `principal.Id.Resource`
- WHAT (group/role): `entitlement.Resource.Id.Resource`
- WHERE (workspace/org): `principal.ParentResourceId.Resource`
- NEVER get context from `entitlement.Resource.ParentResourceId`

In Revoke:
- Principal: `grant.Principal.Id.Resource`
- Entitlement: `grant.Entitlement.Resource.Id.Resource`
- Context: `grant.Principal.ParentResourceId.Resource`

Criteria:
- P1: Entity source correctness per rules above
- P2: Revoke uses grant.Principal and grant.Entitlement correctly
- P3: Grant handles "already exists" as success; Revoke handles "not found" as success. Exception: if the vendor API is natively idempotent (returns success for add-when-already-member or remove-when-not-member), additional `GrantAlreadyExists`/`GrantAlreadyRevoked` handling is unnecessary — only flag if the API returns a distinguishable error that's being swallowed.
- P4: Validate params before API calls; wrap errors with gRPC status codes
- P5: API argument order — multiple string params are easy to swap (verify against function signature)
- P6: ParentResourceId nil check before access

When provisioning files change, read the FULL file content (not just diffs) — entity source correctness requires understanding the complete Grant/Revoke flow.

### Breaking Changes

- B1: Resource type Id: field changes (grants orphaned)
- B2: Entitlement slug changes in NewAssignmentEntitlement/NewPermissionEntitlement
- B3: Resource ID derivation changes (user.ID→user.Email)
- B4: Parent hierarchy changes (org→workspace)
- B5: Removed resource types/entitlements
- B6: Trait type changes (NewUserResource→NewAppResource)
- B7: New required OAuth scopes
- B8: SAFE (do not flag): display name changes, adding new types, adding trait options, adding pagination

### Config/Dependencies

- G1: conf.gen.go must NEVER be manually edited
- G2: Fields use field.StringField/BoolField from SDK
- G3: Required fields: WithRequired(true); secrets: WithIsSecret(true)
- G4: No hardcoded credentials/URLs; base URL configurable

### Documentation Staleness

If `docs/connector.mdx` exists but is NOT in the changed files, check for staleness — stale docs are a release blocker:

- D1: Capabilities table — Resource types added, removed, or changed sync/provision support
- D2: Connector actions — Action schemas added, removed, or modified
- D3: Credential requirements — Required API scopes or permissions changed
- D4: Configuration fields — Config fields added, removed, or renamed

## Known Safe Patterns (do not flag)

These patterns look suspicious but are correct — the SDK handles them:

| Pattern | Why it's safe |
|---|---|
| No nil check before `connectorbuilder.NewConnector` | Validates internally via type switch |
| No status code check after `uhttp.BaseHttpClient.Do()` | Auto-maps non-2xx to gRPC errors with rate limit info |
| No type validation in Grant/Revoke methods | SDK guarantees correct types from entitlement definition |
| No ActiveSync annotations in List calls | `syncIDClientWrapper` middleware adds them automatically |
| `StaticEntitlements` passing nil resource | SDK associates with all resources of that type at sync time |
| `GrantAlreadyExists`/`GrantAlreadyRevoked` without merging other annotations | Standard convention for these annotation types |

## Top Bug Detection Patterns

1. Pagination: `return resources, "", nil, nil` without conditional = stops after page 1
2. Pagination: `return resources, "next", nil, nil` hardcoded = infinite loop
3. HTTP: defer resp.Body.Close() BEFORE if err != nil = panic
4. HTTP: resp.StatusCode in error path without resp != nil check = panic
5. Type assertion: .(Type) without , ok := = panic
6. Error: log.Print(err) without return = silent data loss
7. Error: fmt.Errorf("...%v", err) should be %w
8. IDs: .Email as 3rd arg to NewUserResource = unstable ID
9. ParentResourceId.Resource without nil check = panic

## Finding Severity

| Severity | Blocks Merge | Use When |
|---|---|---|
| `blocking-security` | Yes | Confident security vulnerability |
| `blocking-correctness` | Yes | Confident bug or crash |
| `suggestion` | No | Uncertain issues, style, edge cases |

**When in doubt, use suggestion.**

## Posting Results

Post inline comments on specific lines using `mcp__github_inline_comment__create_inline_comment`.
Prefix each comment: `🔴 Security:` / `🟠 Bug:` / `🟡 Suggestion:`. Keep to 2-3 sentences.

Post a summary comment with `gh pr comment`:

```
### PR Review: <PR title>

**Blocking Issues: N** | **Suggestions: M**

### Security Issues
<one-liner per finding with file:line, or "None found.">

### Correctness Issues
<one-liner per finding with file:line, or "None found.">

### Suggestions
<one-liner per suggestion with file:line, or "None.">
```

Submit verdict:
- Any blocking findings → `gh pr review --request-changes -b "Blocking issues found — see review comments."`
- Otherwise → `gh pr review --comment -b "No blocking issues found."`
