# PR Review Workflow

Internals and all options for `pr-review.yaml`, `general-pr-review.yaml` and the
`.github/actions/pr-review` composite action. The README carries the short
version and how to call it.

Runs Claude-powered PR review for same-repository pull requests. Fork PRs are skipped
because they do not receive automatic Claude review. Review jobs check out the exact PR
head SHA so Claude can inspect the proposed source tree locally. The action builds its
prompt from a shared base prompt plus optional built-in mixins. The default profile is
`connector`, which adds the connector mixin for repos covered by connector required
workflows or rulesets.

Repos that need non-connector review can use the `general-pr-review.yaml` required
workflow, or reusable workflow callers can pass `review_prompt: general`.

Prompt layers are additive:

1. `base-pr-review.md` applies to every repo.
2. Built-in mixins add shared domain rules. Today, `review_prompt: connector` adds
   `mixins/connector.md`; `review_prompt: general` adds no built-in mixin.
3. A repo-local `.claude/skills/ci-review.md` can add project-specific rules on top
   of the selected profile.

`verdict_mode: judge` adds `verdict-judge.md` between the base prompt and the
mixins, so domain and repo-local criteria keep the last word on review content.

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
mode), then submits a commit-bound formal review linking directly to the
report. Which event that is depends on the `verdict_mode` input (see Verdict
Modes below); on the default `baseline` it is request-changes for blockers or
a neutral comment otherwise, and never an approval. Only after the formal
review exists does CI mark the report
`publication: completed` — a completed report is the report PLUS its formal
result, so a report whose review never landed stays `pending`: preserved and
reconciled by identity for a same-attempt retry, but never the next run's
review-state baseline and never a comment the agent can edit. (`verdict_mode:
none` submits no review, so it has no formal result to wait for and its report
is published `completed` directly.) Only after
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

## Reusable Workflow Calls

Callers of `pr-review.yaml` must pass `ANTHROPIC_API_KEY` as a named secret; do not
inherit unrelated secrets. The called workflow uses the caller's existing
`GITHUB_TOKEN` and declared review permissions.

The optional `summary_marker` input selects a single-line heading such as
`### Connector PR Review Canary:`. Empty uses the selected profile's existing
heading. Overrides cannot embed a different reserved connector, general, or legacy
review heading. Custom headings keep summary/state selection separate; they do not
isolate all inline review feedback or grant additional permissions.

## Verdict Modes

The `verdict_mode` input decides how CI turns the review summary into a formal PR
review.

| Mode | How the event is chosen | Submits |
| --- | --- | --- |
| `baseline` | The `**Blocking Issues: N**` count | `REQUEST_CHANGES` when N > 0, `COMMENT` when N == 0 |
| `judge` | An explicit `**Verdict: ...**` row the reviewer writes under the count | `APPROVE` / `REQUEST_CHANGES` / `COMMENT` |
| `none` | — | nothing; the report is published alone |

Which one you get depends on how the review reaches you:

| Entry point | Mode | Why |
| --- | --- | --- |
| `pr-review.yaml` (the connector profile) | `judge`, subject to the check below | A bot approval cannot satisfy the human gate on a connector repo (below), so its only effect there is clearing the bot's own block, which is the whole point |
| `general-pr-review.yaml` | `baseline` | It passes no `verdict_mode`, so the action's own conservative default applies |
| the action called directly | `baseline` | Same |

**Judge mode is conditional, not just configured.** Asking for `judge` is a
request, and the action grants it only after reading the base branch's
`required_approving_review_count` at the start of the run. Anything other than
a confirmed zero runs `baseline` instead and says so in the job log — a branch
requiring approvals, and equally a requirement it could not read. So a repo
that starts requiring an approval in a ruleset stops getting bot approvals on
its next run, with no one having to notice and reconfigure it. A requirement
the check cannot see is the one exception, and it has its own paragraph below;
read that before relying on this one.

What counts as a confirmed zero is deliberately narrow, because the evidence
available to `GITHUB_TOKEN` is narrow. It can read rulesets
(`/rules/branches/{branch}`) but **not** classic branch protection
(`/branches/{branch}/protection` answers 403 for this token). Several rulesets
can apply to one branch at once — org- and repo-level — so the strictest
requirement among them wins rather than the first one listed. A branch with no
ruleset requirement at all is a confirmed zero only when it is also
unprotected: protection that no ruleset explains could be a classic
protection, so it reads as unknown.

**The limit of the check**, stated plainly because it is not obvious: it sees
rulesets. One shape escapes it — a classic branch protection requiring
approvals *alongside* a ruleset `pull_request` rule that requires none. The
ruleset's zero is read as the answer, and the bot can approve into the classic
requirement. (A branch protected *only* by classic protection does not escape:
with no ruleset rule to explain the protection, it reads as unknown and loses
`judge`.)

Closing that shape would mean granting the review workflow
`administration: read` on every repo it runs in — a larger and more permanent
privilege than the one it would be protecting — so the trade is deliberately
refused.

A repo in that shape opts out by **expressing the requirement where the check
can see it**: add a ruleset `pull_request` rule requiring at least one
approval, and the next run downgrades itself to `baseline`. That is the whole
opt-out, and it is the same action the repo would take to make the
requirement apply uniformly anyway. Note that connector repos cannot opt out
by passing an input — they run this workflow from an org rule rather than
calling it, so `inputs` is empty and the `judge` fallback always applies. Only
a repo that calls `pr-review.yaml` itself can pass `verdict_mode: baseline`.

That is what lets the connector profile default to `judge` safely. It is also
why `github-workflows` itself — the one repo here that requires an approving
review — would fall back even if it were on that profile; as it happens it is
reviewed by `general-pr-review.yaml` and never asks for `judge` at all.

In judge mode `APPROVE` and `REQUEST CHANGES` map straight through. `DEFER`
declines to answer, so it falls back to the baseline mapping of its own count
row: a neutral comment at zero blockers, request-changes above. Judge is
therefore never less blocking than baseline would have been for the same
summary — deferring is a statement about coverage, not a way to unblock.

Baseline deliberately has no `APPROVE` path. On GitHub a `COMMENT` review never
clears a reviewer's earlier `CHANGES_REQUESTED` — only a later `APPROVE` from that
reviewer, or a human dismissal, does. So under baseline a blocking finding keeps the
PR blocked after the author fixes it until someone dismisses the review by hand.
Judge mode is what lets a clean re-run clear its own earlier block.

Judge mode is not "baseline that can approve". It asks the reviewer for a verdict
with three values, and `DEFER` is the one that makes approval safe: an `APPROVE`
satisfies the PR's review requirement for the *whole* change, so a review that found
nothing blocking but could not cover the whole diff defers to a human instead of
approving by default. An `APPROVE` over a positive blocking count is rejected as a
contradiction and fails the run.

`none` is for callers that want the report and the inline findings without the bot
holding a GitHub reviewer state at all. It still writes the `review-state` marker
that drives incremental review, and still requires a parseable count row: that row
is the only structural proof the agent finished its summary, so without it a
truncated run would publish as completed state and silently retire commits nobody
reviewed.

The one outcome judge mode adds over baseline is `APPROVE`. Everything else is
baseline's mapping or stricter: `REQUEST CHANGES` can block at a zero count,
and `DEFER` resolves to whatever baseline would have submitted.

### What judge mode costs, and why it is acceptable on connector repos

None of the below are new code paths — they are what an approving review means
on GitHub. They are listed because the connector profile now defaults to
`judge`, and because they are exactly what changes if a repo on that profile
ever starts requiring approvals.

- **The bot gets a vote — but not the deciding one.** The review is submitted
  with `GITHUB_TOKEN`, so an `APPROVE` is recorded as an approving review by
  `github-actions[bot]` and counts toward a `required_approving_review_count`
  rule. Connector repos set that count to 0 and gate merges a different way:
  their ruleset carries a `required_reviewers` entry naming a team, with
  `minimum_approvals: 1`, over every file but `docs/connector.mdx`. A bot is
  not a member of that team, so **its approval cannot satisfy the human gate**
  — the only thing it can do is clear the bot's own `CHANGES_REQUESTED`. A
  connector PR still needs a person from that team to approve it.

  The run-time check above contains the other direction: a repo that *does*
  put its requirement in `required_approving_review_count` gets `baseline`
  from the moment the requirement becomes visible to the check, without anyone
  having to reconfigure the review — with the single exception carved out in
  *The limit of the check* above, where the requirement is invisible to it.
  (The check reads that count and not `required_reviewers`, which is correct
  rather than an oversight: a named-team requirement is one a bot cannot
  satisfy in the first place, so there is nothing to withhold.)
- **Approvals are not self-retracting.** Nothing in this action dismisses a
  review. Once an `APPROVE` lands for commit A it stays the bot's latest review
  until some later run submits a different event — and no later run submits one
  if the review step fails, is cancelled, or the caller switches to `none`.
  Connector repos do not have "dismiss stale pull request approvals when new
  commits are pushed" enabled, so a stale approval does persist; at 0 required
  approvals it is inert, but it would not be on a repo that required any.
- **It raises the ceiling on an existing weakness.** The verdict is parsed out
  of a comment, and publication selects that comment by author login, heading,
  and freshness — not by an id bound before the agent ran. A user with write
  access can edit a `github-actions[bot]` comment, and on a same-repo PR the
  author has write access by construction; an edit refreshes `updated_at`, so
  it passes the freshness gate. Under `baseline` the most that buys is
  suppressing a block (editing the count to 0 downgrades to `COMMENT`). Under
  `judge` the same edit manufactures an `APPROVE`. The same is true of prompt
  injection from PR file content, which the base prompt treats as untrusted for
  the title and body but not for the diff. On a connector repo that buys the
  editor nothing they lack — anyone with write can already dismiss the bot's
  review by hand, which is how every such block has been cleared so far. It
  would buy them something real on a repo requiring approvals, which is the
  second reason such a repo belongs on `baseline`. Closing it properly means
  refusing a summary whose last editor was not the bot (GraphQL exposes
  `editor` and `lastEditedAt` on an issue comment).

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
