# 617 — AutoGen Migration Guide

**Role:** docs
**Priority:** 2 (high)
**Scope:** small
**Depends on:** none

## Problem

AutoGen entered maintenance mode in February 2026, leaving thousands of users searching for a new multi-agent framework. There is no migration guide from AutoGen to Bernstein. This is a time-sensitive content opportunity that will lose relevance as users settle on alternatives.

## Design

Create a comprehensive "Migrating from AutoGen to Bernstein" guide. Map AutoGen concepts to Bernstein equivalents: AssistantAgent -> worker agent, GroupChat -> orchestration run, ConversableAgent -> agent adapter. Provide code examples showing AutoGen patterns and their Bernstein equivalents. Cover the key differences: Bernstein uses CLI agents (not SDK agents), file-based state (not in-memory), and deterministic orchestration (not LLM-based routing). Include a FAQ addressing common AutoGen user concerns. Publish as a standalone doc and promote on r/AutoGen and relevant Discord channels.

## Files to modify

- `docs/migrations/from-autogen.md` (new)
- `README.md` (link in "Coming from..." section)

## Completion signal

- Migration guide exists with concept mapping and code examples
- FAQ addresses top 5 AutoGen user concerns
- Guide is linked from README
