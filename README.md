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
| `case-studies/` | Case study pages, plus the tariff advisor's chat widget frontend (`tariff-advisor-widget.js`) |
| `styles.css` | Shared styling for the whole site |
| `favicon.svg`, `icons/`, `images/`, `profile.webp` | Site icons, favicon concepts, and diagrams used in the case studies |
| `tariff-advisor/` | A separate Python project: the Octopus Tariff Advisor's recommendation engine, CLI, MCP server and web chat backend. See [tariff-advisor/README.md](tariff-advisor/README.md). |

## Case studies

- [Octopus Tariff Advisor](https://ggfrom34.github.io/GG-portfolio-website/case-studies/octopus-tariff-advisor.html) — an AI agent providing information support for UK homeowners, with a live chat demo.
- [Building this site](https://ggfrom34.github.io/GG-portfolio-website/case-studies/building-this-site.html) — a meta case study on how this site itself was built with Claude Code.

## Deployment

Every push to `main` redeploys via GitHub Pages (Settings → Pages → Deploy
from a branch). The tariff advisor's backend deploys separately to Render
(see `render.yaml` and
[tariff-advisor/README.md](tariff-advisor/README.md#web-chat-public-site)).
