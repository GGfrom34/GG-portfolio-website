# PRD to Jira

*Work in progress — this README will be filled in once the workflow below has been run end-to-end against a real Jira Cloud project. See [`Case study material/prd-to-jira-architecture.md`](Case%20study%20material/prd-to-jira-architecture.md) for the design decisions made so far.*

A two-agent, human-gated workflow for turning a PRD into a structured Jira Epic and its Story/Task issues:

1. **[`prd-quality-reviewer`](../.claude/agents/prd-quality-reviewer.md)** — a Claude Code subagent that reads an uploaded PRD and reports quality gaps (missing success metrics, unclear scope, missing acceptance criteria, etc.). It never edits the PRD.
2. **[`improve-prd`](../.claude/skills/improve-prd/SKILL.md)** — a Claude Code skill that walks through fixing the gaps the reviewer found, one at a time, with your sign-off on each change.
3. **[`jira-epic-builder`](../.claude/agents/jira-epic-builder.md)** — a second subagent, used only once you've explicitly told Claude the PRD is approved. It asks which Jira project the work belongs to, proposes an Epic + Story/Task breakdown, and creates it via [Atlassian's official remote MCP server](https://mcp.atlassian.com/v2/mcp) once you confirm the breakdown.

Nothing reaches Jira without two explicit human checkpoints: PRD sign-off, then breakdown sign-off.
