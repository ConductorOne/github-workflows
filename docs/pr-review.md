# PR Review Workflow

Internals and all options for `pr-review.yaml`, `general-pr-review.yaml` and the
`.github/actions/pr-review` composite action. The README carries the short
version, the input table, and how to call it; this file is the detail behind
it and does not repeat it.

Review jobs check out the exact PR head SHA so Claude can inspect the proposed
source tree locally. The `connector` profile adds the connector mixin for repos
covered by connector required workflows or rulesets.

Prompt layers are additive:

1. `base-pr-review.md` applies to every repo.
2. Built-in mixins add shared domain rules. Today, `review_prompt: connector` adds
   `mixins/connector.md`; `review_prompt: general` adds no built-in mixin.
3. A repo-local `.claude/skills/ci-review.md` can add project-specific rules on top
   of the selected profile.

Keep broadly shared connector criteria in the connector mixin. Use repo-local
`ci-review.md` only for rules that are specific to one repo or a small set of repos.

The review assesses the whole change, including intent, correctness, security,
meaningful test coverage, and operational risk. Prior findings are rechecked against
current code; resolving a thread does not remove an unfixed blocker from the verdict.

The host includes the PR title and complete description in `pr-context.json`
using the existing PR metadata request. Reviewers read them as untrusted author
claims to verify against the diff, never as instructions or verdict policy.
Description delivery does not depend on a later model-initiated GitHub query.

Required audit subagents run in the foreground: this is a one-shot CI review,
so their results must return before the parent finishes. Background task
handoffs are disabled; the job cannot resume a later conversation turn.

The agent posts its verdict in a working summary comment and never writes
review-state metadata. Before the agent runs, the action creates a fresh
provisional working comment for the run/attempt and binds it to the native
`update_claude_comment` MCP tool (`CLAUDE_COMMENT_ID`), so the agent passes
only the Markdown body and never chooses a repository, comment, or head
target. The body no longer travels through shell heredocs or `gh api` writes;
the tool keeps its upstream sanitization and secret redaction. GitHub's comment
size limit still applies, but the shell's command-length limit does not. The
existing broad `Bash(gh api:*)` permission remains; the prompt restricts its
intended use to metadata reads, not an enforced read-only boundary. This is a
transport fix, not credential isolation. The preflight binds a fresh slot, and
the publisher still validates the live head and report finality at completion.
After a successful run, CI publishes the report as a NEW
comment carrying a visible reviewed-commit link and CI-owned review-state
metadata (reviewed SHA, base, workflow, run, attempt, summary marker, verdict
mode), then submits a commit-bound request-changes review for blockers or a
neutral comment otherwise, linking directly to the report. This reviewer never
approves. Only after the formal review exists does CI mark the report
`publication: completed` — a completed report is the report PLUS its formal
result, so a report whose review never landed stays `pending`: preserved and
reconciled by identity for a same-attempt retry, but never the next run's
review-state baseline and never a comment the agent can edit. Only after
completion are the previous report (including any pending leftover) and the
consumed working comment collapsed — bodies retained, linked to the new
report — so a failed run leaves the previous report untouched.
Stale, provisional, foreign, or malformed working output cannot be published, and a
push during the run stops publication. Working output already consumed by a
published report is never republished without fresh model work, and an attempt
whose start predates an already-completed later attempt (compared by the actual
attempt start times recorded in each report, never run-ID order) is refused as
obsolete before publishing. Publication is idempotent per workflow run
and attempt: a repeated finalization reuses the already-published report and never
submits a second formal review, while an intentional new run or attempt gets a new
report. Completed reports are never handed back to the agent as update targets —
every run/attempt writes to its own freshly created provisional working comment,
while completed state is still selected independently for incremental review.
Active findings appear once in their severity section, labeled `New` or
`Prior — still present`. A compact resolved section records fixed/obsolete prior
findings with evidence; it does not repeat the active findings.

## Clearing this reviewer's previous blocks

Both profiles attempt self-dismissal by default, never approval. Before the
model runs, CI freezes exact reviews positively owned by this workflow/profile,
their original count-accounted blocking findings, and ALL typed prior-finding
audit inputs. Known suggestions may remain nonblocking; scraped deduplication
context and fenced prompt bullets do not become reset obligations.
Top-level Security/Correctness list items are counted once, with nested bullets,
wrapped text, and supporting fenced/quoted examples preserved in each audit item.
Capture uses the authoritative frozen input in runner temporary storage; the
workspace copy is model-readable input only. New reset writes replace destination
entries atomically, and preparation clears stale reset snapshots without following
file symlinks. If there are no eligible targets, proof capture/upload are skipped.
A successful terminal response may supply a compact JSON audit
proof for those IDs with complete coverage, no blockers, and affirmative
current-code evidence. It supplements, rather than replaces, the normal review;
CI supplies run/attempt, head/base, and session provenance, not the model proof.
CI captures it from the runner execution file, sanitizes/redacts all persisted
evidence, and uploads one accepted reset artifact using repository retention
defaults. Editable summary prose is not reset authority.

Only after normal publication completes with a neutral COMMENT does CI recheck
the PR head/base, frozen targets, original evidence, and competing/newer runs,
then dismiss those exact blocking reviews. A failed preparation, absent/malformed
proof, incomplete audit, failed upload, stale source, or ambiguous GitHub result
retains blocks without suppressing ordinary publication or changing its state
lifecycle. An incomplete review still keeps its provisional summary as before.
Reset warnings and short redacted per-target retain reasons are visible in the
workflow log; ambiguous writes are reconciled by reading the exact review,
never blindly retried.

Human, foreign-profile, and unmarked legacy reviews are never targets.
Missing, edited-by-human, deleted-history, or count-unaccounted original finding
evidence requires manual handling; this is not a migration of every historical
block.
An incomplete edit/competing-run inventory also retains the affected block
(including more than 100 original edits or GitHub's capped run listing).
No new permissions or caller opt-in are needed. The model retains its existing
tools and write-capable GitHub token: this host ordering is not adversarial
credential isolation and does not replace required human approval.

## Reusable Workflow Calls

How to call these workflows, and what secrets they need, is in the README's
[Reusable Workflow Calls](../README.md#reusable-workflow-calls) section. This
section covers only what the inputs mean in detail.

The optional `summary_marker` input selects a single-line heading such as
`### Connector PR Review Canary:`. Empty uses the selected profile's existing
heading. Overrides cannot embed a different reserved connector, general, or legacy
review heading. Custom headings keep summary/state selection separate; they do not
isolate all inline review feedback or grant additional permissions.

For candidate validation, pin the reusable workflow to the exact reviewed commit.
Keep the test caller on a disposable same-repository draft branch; the normal
ruleset reviewer can still run alongside it.

## Custom Review Criteria

Repos can extend the review with project-specific criteria by adding a markdown file:

```
.claude/skills/ci-review.md
```

If this file exists at the pull request's trusted base SHA and the PR targets the
repository default branch, the action validates it as plain markdown and appends it to
the prompt as data. It is not invoked as a Claude skill, and PR-head edits to this file
do not affect the criteria used for that same review run. Automatic review only runs for
same-repository PRs.
