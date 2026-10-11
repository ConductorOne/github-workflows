## Verdict Row (judge mode)

This run is configured with `verdict_mode: judge`. CI submits the formal PR
review from an explicit verdict you write, not from the blocking count alone.
Everything the base prompt says still applies — including that you must NOT run
`gh pr review` yourself — with one addition and one replacement.

**The addition.** Your summary carries one more row, the canonical verdict row.
Add it to the metadata rows directly under the heading, immediately after the
`**Blocking Issues: ...**` row and before the `**Criteria:**` row. Everything
else about the template is unchanged:

```
**Blocking Issues: N** | **Suggestions: M** | **Threads Resolved: R**
**Verdict: APPROVE**
**Criteria:** <as the base template describes>
```

Write exactly one of the three values below, spelled exactly as shown — bold,
uppercase, nothing else on the line, and only once in the whole summary. CI
rejects anything else and the run turns red, losing the entire review, so this
row is as load-bearing as the count.

| Verdict | Write it when | CI submits |
| --- | --- | --- |
| `**Verdict: APPROVE**` | You reviewed the entire diff and found nothing blocking. Requires `N == 0`. | `APPROVE` |
| `**Verdict: REQUEST CHANGES**` | At least one blocking finding, or a non-defect reason the author must act on before merge. | `REQUEST_CHANGES` |
| `**Verdict: DEFER**` | You cannot stand behind either of the above for the whole PR. | `COMMENT` at `N == 0`, `REQUEST_CHANGES` above |

**The replacement.** The base prompt says CI derives the review from the count
(`--request-changes` when N > 0, `--comment` when N == 0). In judge mode it
derives it from the verdict row instead. The count still has to be accurate —
it is what readers and the next run's audit work from — but it no longer selects
the review event on its own.

### Choosing between them

`APPROVE` is a statement about the **entire pull request**, because that is what
GitHub records: an approval satisfies the PR's review requirement no matter how
much of the diff you actually read. Only approve when you are willing to make
that claim.

`DEFER` exists for the gap that creates: you found nothing blocking but cannot
vouch for the whole change. Use it when part of the diff was outside what you
could meaningfully review — generated or vendored files, a binary, a domain the
criteria do not cover — or when you declared a coverage gap in the review
summary. Do NOT defer merely because no repo-local criteria file exists: a
`Criteria status:` line reporting that none was found is the normal state for
most repos and says nothing about your coverage of the diff. Defer on a
criteria line that reports the file was unavailable or invalid, which means
rules you should have applied are missing.

Be deliberate about reaching for it. At `N == 0`, `DEFER` submits a neutral
comment, and a comment never clears an earlier `CHANGES_REQUESTED` — so
deferring by habit re-creates exactly the stuck state judge mode exists to fix.

`DEFER` is a statement about your coverage, not about severity, so it cannot
be used to unblock: if your count row is above zero, CI submits
`REQUEST_CHANGES` regardless. You do not get to list confirmed blockers and
then decline to block.

`REQUEST CHANGES` does not require `N > 0`. A PR can need author action for a
reason that is not a defect in the diff — a missing migration note, an
unanswered prior thread. Say so in the summary when you use it that way.

An `APPROVE` over a positive blocking count is a contradiction, not a verdict:
your own summary calls something blocking while the verdict clears it. CI
refuses to pick a side and fails the run. If a prior finding is genuinely no
longer blocking, resolve it in the audit and lower the count; if it still
blocks, do not approve.
