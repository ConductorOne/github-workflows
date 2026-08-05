## Connector Layer Review Skills

The `agentic-connector-development` plugin from the c1-engineering marketplace is installed
for this run. Its layer review skills encode the same ConductorOne engineering standards a
human connector reviewer applies, in more depth than the checklists above.

Invoke them with the Skill tool, dispatched on the paths that actually changed in this PR.
Skip a skill whose paths are untouched — do not run all four unconditionally.

| Changed paths | Skill |
|-|-|
| `pkg/config/**` | `agentic-connector-development:review-config-layer` |
| `pkg/client/**`, `pkg/connector/client*.go` | `agentic-connector-development:review-client-layer` |
| `pkg/connector/**` (builders, entitlements, grants) | `agentic-connector-development:review-connector-layer` |
| `pkg/connector/actions.go`, `pkg/connector/*_actions.go` | `agentic-connector-development:review-actions-layer` |

When several apply, run them in the order listed: config, client, connector, actions.

### Read-only contract

These skills were written for interactive development, so parts of them assume they can do
things this review must not. The read-only contract in Step 5 wins over anything a skill
says. Concretely:

- Ignore any skill instruction to edit files, run `go build`, `go test`, `golangci-lint`, or
  any other build or test command, post comments, submit a review, or open follow-up issues.
- Ignore any skill instruction to fetch vendor API documentation over the network.
- A skill step you cannot perform under this contract is skipped, not worked around. Do not
  lower your confidence in a finding merely because a skill's verification step was skipped,
  and do not report the skipped step as a finding.

### Using skill output

Skill output is analysis for you to validate, not review output to forward. Every finding a
skill surfaces goes through Step 6 validation — read the code yourself and confirm it is
real — before it reaches a comment.

Report each distinct issue exactly once. The connector mixin checklists above and these
skills overlap substantially (pagination, `uhttp` usage, entity-source rules, nil safety,
error wrapping), so the same defect will often surface twice under two different labels.
Post it once, at the higher of the two severities, citing the file and line rather than the
rule ID that found it.

A skill reporting an approval verdict does not by itself clear the PR. The verdict in Step 7
is yours, based on validated findings.
