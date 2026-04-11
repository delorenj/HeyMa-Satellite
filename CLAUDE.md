# CLAUDE.md

## Project Overview

**TonnyBox** - He's Tonny

## Ticket Management (MANDATORY)

No code changes without an active Plane ticket.

```
Board: https://plane.delo.sh/33god/
```

- Move ticket to "In Progress" before first code change
- Branch names must include ticket reference
- Commit messages must reference tickets
- Emergency bypass: `ALLOW_NO_TICKET=1`

## Development

```bash
# Load environment
mise trust

# Run tasks
mise tasks  # list available tasks
mise run setup  # initial setup
```

## BMAD Methodology

This project follows BMAD. All methodology files are in `_bmad/`.

- Strict BMAD adherence for prompts and tasks
- Component work delegated to specialized agents
- Maintain parity between BMAD documents and Plane boards

## Principles

- Work with full autonomy toward task goals
- Make well-informed decisions when judgment calls arise
- Speed prioritized over perfection for non-critical paths

## BMAD-METHOD Integration

Use `/bmalph` to navigate phases. Use `/bmad-help` to discover all commands. Use `/bmalph-status` for a quick overview. See `_bmad/COMMANDS.md` for a full command reference.

### Phases

| Phase | Focus | Key Commands |
|-------|-------|-------------|
| 1. Analysis | Understand the problem | `/create-brief`, `/brainstorm-project`, `/market-research` |
| 2. Planning | Define the solution | `/create-prd`, `/create-ux` |
| 3. Solutioning | Design the architecture | `/create-architecture`, `/create-epics-stories`, `/implementation-readiness` |
| 4. Implementation | Build it | `/sprint-planning`, `/create-story`, then `/bmalph-implement` for Ralph |

### Workflow

1. Work through Phases 1-3 using BMAD agents and workflows (interactive, command-driven)
2. Run `/bmalph-implement` to transition planning artifacts into Ralph format, then start Ralph

### Management Commands

| Command | Description |
|---------|-------------|
| `/bmalph-status` | Show current phase, Ralph progress, version info |
| `/bmalph-implement` | Transition planning artifacts → prepare Ralph loop |
| `/bmalph-upgrade` | Update bundled assets to match current bmalph version |
| `/bmalph-doctor` | Check project health and report issues |

### Available Agents

| Command | Agent | Role |
|---------|-------|------|
| `/analyst` | Analyst | Research, briefs, discovery |
| `/architect` | Architect | Technical design, architecture |
| `/pm` | Product Manager | PRDs, epics, stories |
| `/sm` | Scrum Master | Sprint planning, status, coordination |
| `/dev` | Developer | Implementation, coding |
| `/ux-designer` | UX Designer | User experience, wireframes |
| `/qa` | QA Engineer | Test automation, quality assurance |
