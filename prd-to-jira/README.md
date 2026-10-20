# PRD to Jira

A two-agent, human-gated workflow that turns a PRD into a structured Jira Epic and its Story/Task issues. It has been run end to end on a real PRD, producing one Epic and five linked issues in a Jira Cloud project. The write-up is on the portfolio site: [PRD to Jira Workflow case study](https://ggfrom34.github.io/GG-portfolio-website/case-studies/prd-to-jira-workflow.html).

## The pieces

1. **[`prd-quality-reviewer`](../.claude/agents/prd-quality-reviewer.md)** is a Claude Code subagent that reads a PRD against an eight-dimension rubric and reports gaps by severity (blocker, gap, nice-to-have). It has no write tools and never edits the PRD.
2. **[`improve-prd`](../.claude/skills/improve-prd/SKILL.md)** is a Claude Code skill that works through the reviewer's gaps one at a time. For each, it asks a targeted question, shows a before/after, and only edits the PRD once you approve it.
3. **[`jira-epic-builder`](../.claude/agents/jira-epic-builder.md)** is a second subagent, used only after you have said the PRD is final. It asks which Jira project to use, proposes an Epic plus Story/Task breakdown, and creates the issues through [Atlassian's official remote MCP server](https://mcp.atlassian.com/v2/mcp) once you approve the breakdown.

Nothing reaches Jira without two explicit human checkpoints: PRD sign-off, then breakdown sign-off.

## Running it

1. Register the Atlassian MCP server (already configured in [`.mcp.json`](../.mcp.json), which holds a URL and no secrets) and sign in once with `claude mcp login atlassian`.
2. Share a PRD in Claude Code and ask `prd-quality-reviewer` to review it.
3. Work through the gaps with `improve-prd`, then ask the reviewer to check the updated PRD again.
4. When you are happy with it, say the PRD is final and ask for it to be created in Jira. `jira-epic-builder` takes it from there.

## What is in this folder

Everything is in [`Case study material/`](Case%20study%20material/):

| File | What it is |
|---|---|
| [`prd-to-jira-architecture.md`](Case%20study%20material/prd-to-jira-architecture.md) | Why the work is split this way (two agents and a skill, two gates, the official MCP server) |
| [`sample-prd.md`](Case%20study%20material/sample-prd.md) | A PRD with three deliberately planted gaps, listed at the bottom of the file |
| [`test-prd.md`](Case%20study%20material/test-prd.md) | The same PRD without that note, for testing a reviewer blind |
| [`improved-prd.md`](Case%20study%20material/improved-prd.md) | The sample after two review-and-fix rounds, and the PRD that was turned into Jira issues |
| [`PRD to Jira case study.md`](Case%20study%20material/PRD%20to%20Jira%20case%20study.md) | The case study copy |

## Result of the end-to-end run

The first review of `sample-prd.md` flagged 11 gaps. All were fixed with individually approved edits. A second review of the fixes caught four more, which were also fixed. The final PRD became one Epic, three Stories and two Tasks, and each issue was read back from Jira to confirm its parent link.
