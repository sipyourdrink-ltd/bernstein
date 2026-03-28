# 712 — Discord Community Setup

**Role:** docs
**Priority:** 1 (critical)
**Scope:** small
**Depends on:** none

## Problem

Every successful open-source tool has a Discord. CrewAI, LangChain, Cursor — all have active Discords. It's where users help each other, report issues, share configs, and feel part of something. No Discord = no community = no organic growth.

## Design

### Channels
- `#general` — discussion
- `#show-and-tell` — share what you built with bernstein
- `#help` — support
- `#adapters` — adapter development (aider, cursor, etc.)
- `#feature-requests` — ideas
- `#benchmarks` — share benchmark results
- `#dev` — contributor discussion
- `#announcements` — releases only

### Bot
- GitHub webhook → `#announcements` for releases
- Star count display in channel topic

### README addition
Add Discord badge + invite link to README header badges.

## Files to modify

- `README.md` (add Discord badge)
- `docs/CONTRIBUTING.md` (mention Discord)

## Completion signal

- Discord server exists with channel structure
- Invite link in README
- GitHub webhook connected
