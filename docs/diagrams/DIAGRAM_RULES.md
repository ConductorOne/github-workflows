# Diagram Maintenance Rules

Rules for maintaining the release workflow diagram.

> **For AI Coding Agents (Cursor, Claude Code, etc.):**
>
> When modifying `.github/workflows/release.yaml`, you MUST:
>
> 1. Read `docs/release-workflow.md` for context on the pipeline
> 2. Update the diagram (`docs/diagrams/release-workflow.dot`) if jobs change
> 3. Update `docs/release-workflow.md` if security properties or outputs change
> 4. Run `make docs` to regenerate the PNG
>
> See "When to Update" below for specific triggers.

## Files

| File                                 | Purpose                                                           |
| ------------------------------------ | ----------------------------------------------------------------- |
| `docs/release-workflow.md`           | Pipeline overview, job descriptions, security properties, testing |
| `docs/diagrams/release-workflow.dot` | Diagram source (Graphviz DOT)                                     |
| `docs/diagrams/release-workflow.png` | Generated diagram (do not edit directly)                          |

## Generation

```bash
make docs
```

Or manually:

```bash
dot -Tpng docs/diagrams/release-workflow.dot -o docs/diagrams/release-workflow.png
```

## When to Update

### Diagram (`release-workflow.dot`)

Update when:

1. **Jobs added/removed** from `release.yaml`
2. **Job dependencies change** (the `needs:` field)
3. **New output destinations** added (new registries, storage)

### Documentation (`release-workflow.md`)

Update when:

1. **New attestation types** are added
2. **Signing or verification** process changes
3. **Directory structure** changes
4. **New security properties** are introduced

## Style Conventions

### Node Shapes

- Jobs: `shape=box, style="rounded,filled"`
- External systems: same, different colors

### Colors

- Trigger: `#fef3c7` (amber)
- Jobs: `#ecfeff` (cyan) or `#f9fafb` (gray)
- Verification: `#f0fdf4` (green)
- AWS resources: `#fef9c3` (yellow)

### Labels

- Use `\n` to separate lines
- Keep to 3-4 bullet points max
- Focus on "what" not "how"

### Naming

- Use lowercase with underscores: `determine_ref`, `binaries`
- Group related nodes in clusters

## Validation

After changes:

1. Run `make docs` to regenerate
2. Check the PNG renders correctly
3. Verify left-to-right/top-to-bottom flow is clear
4. Ensure no isolated nodes
