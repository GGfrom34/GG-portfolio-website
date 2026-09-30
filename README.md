# GG Portfolio Website

Guillaume Goujon's portfolio site: case studies in building with Claude Code
and AI agents.

**Live site:** https://ggfrom34.github.io/GG-portfolio-website/

## What's here

Plain HTML/CSS/JS, no build step, no framework — deployed straight from
`main` via GitHub Pages.

| Path | What it is |
|---|---|
| `index.html` | Home page |
| `case-studies/` | Case study pages, plus the chat widget frontends for the two live demos (`tariff-advisor-widget.js`, `support-assistant-widget.js`) |
| `styles.css` | Shared styling for the whole site |
| `favicon.svg`, `icons/`, `images/`, `profile.webp` | Site icons, favicon concepts, and diagrams/screenshots used in the case studies |
| `tariff-advisor/` | A separate Python project: the Octopus Tariff Advisor's recommendation engine, CLI, MCP server and web chat backend. See [tariff-advisor/README.md](tariff-advisor/README.md). |
| `octopus-support-assistant/` | A separate Python project: an independent, unofficial Octopus Energy customer-support chat assistant — core logic, CLI, and web chat backend. See [octopus-support-assistant/README.md](octopus-support-assistant/README.md). |
| `prd-to-jira/` | A two-agent Claude Code workflow (not a standalone app) that turns a PRD into a structured Jira Epic and issues. See [prd-to-jira/README.md](prd-to-jira/README.md). |

## Case studies

- [Octopus Tariff Advisor](https://ggfrom34.github.io/GG-portfolio-website/case-studies/octopus-tariff-advisor.html) — an AI agent providing information support for UK homeowners, with a live chat demo.
- [AI customer support live chat](https://ggfrom34.github.io/GG-portfolio-website/case-studies/octopus-support-assistant.html) — an independent, unofficial customer-support chat assistant that only answers from real, live-fetched help pages, with a live chat demo.
- [PRD to Jira Workflow](https://ggfrom34.github.io/GG-portfolio-website/case-studies/prd-to-jira-workflow.html) — a two-agent, human-gated workflow that turns a PRD into a structured Jira Epic and issues.
- [Building this site](https://ggfrom34.github.io/GG-portfolio-website/case-studies/building-this-site.html) — a meta case study on how this site itself was built with Claude Code.
- [Weekly AI Competitive Briefing](https://ggfrom34.github.io/GG-portfolio-website/case-studies/weekly-ai-briefing.html) — a hard-gated, multi-agent Cowork pipeline that turns two fast-moving AI markets into a verified weekly briefing.

## Deployment

Every push to `main` redeploys via GitHub Pages (Settings → Pages → Deploy
from a branch). The tariff advisor's and support assistant's backends each
deploy separately to Render as their own service (see `render.yaml`,
[tariff-advisor/README.md](tariff-advisor/README.md#web-chat-public-site),
and
[octopus-support-assistant/README.md](octopus-support-assistant/README.md#web-chat-public-site)).
