# Architecture decisions — PRD to Jira

## Why two agents and a skill, not one agent that does everything

`prd-quality-reviewer` and `jira-epic-builder` are kept as separate subagents (isolated context each) rather than one combined agent, and `improve-prd` is kept as a skill (runs inline in the main conversation) rather than a third agent. The split maps to a real division of responsibility, not just code organisation:

- The **reviewer** only ever reads and critiques. Giving it write tools or Jira access at all would blur the one guarantee it makes: it cannot have changed the PRD or created anything by the time it hands back.
- **`improve-prd`** is the only place PRD text is edited, and it runs in the main thread (not an isolated subagent) so you see and approve every change as it happens, inline.
- The **epic builder** only runs after you explicitly say the PRD is signed off. Keeping it a separate agent means its tool access (Jira write calls) is scoped to a narrow, deliberately-invoked context, not available by default in a general conversation where a stray instruction could otherwise trigger a Jira write.

## Why one `improve-prd` skill instead of a library split by PRD section

Considered splitting into per-dimension skills (e.g. `improve-prd-success-metrics`, `improve-prd-scope`). Rejected for v1: a PRD's gaps are usually related (a fuzzy scope usually also means fuzzy acceptance criteria), and a single skill sharing one rubric with the reviewer keeps both pieces speaking the same vocabulary. Revisit only if a specific dimension turns out to need materially deeper, reusable treatment than the general rubric gives it — see `.claude/skills/add-tariff-family/SKILL.md`'s "Step 0: classify first" pattern as a precedent for how this repo prefers explicit classification over premature polymorphism.

## Why Atlassian's official remote MCP server instead of a custom one

`tariff-advisor/mcp_server.py` is a custom stdio server this project's author wrote from scratch. That pattern doesn't apply here: Atlassian ships a pre-built, OAuth-authenticated remote server (`https://mcp.atlassian.com/v2/mcp`, streamable HTTP) that already exposes Jira read/write tools scoped to whatever the authenticated Atlassian account can access. There is nothing to build — only a URL to register (`.mcp.json`, no secrets, safe to commit) and a one-time per-user OAuth login (`claude mcp login atlassian`, credentials never touch the repo).

## Why the epic builder queries issue types at runtime instead of hardcoding "Story"/"Task"

Jira Cloud's free tier issue-type defaults differ by project template: team-managed Kanban ships "Task" as the child type, team-managed Scrum ships "Story", and company-managed projects use shared, admin-controlled schemes that can differ project to project. Hardcoding either name risks a silent failure (the create call errors, or worse, an admin has renamed the type) that only appears when it's too late to matter. The builder always calls the project's issue-type metadata first and proposes issue types that are confirmed to exist in the chosen project.

## Two sign-off gates, not one

The reviewer→improve→builder flow has two separate human checkpoints, not one combined "are you sure?":

1. **PRD sign-off** — you decide the PRD itself is ready. This is a judgment call about the *content*.
2. **Breakdown sign-off** — even with an approved PRD, the mapping from PRD sections to individual Jira issues is itself a judgment call the agent can get wrong (e.g. splitting one feature into too many or too few stories). Collapsing this into the PRD sign-off would mean approving an issue breakdown you haven't actually seen yet.
