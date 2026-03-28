# 335d — Agent Lesson Propagation System
**Role:** backend  **Priority:** 2 (high)  **Scope:** medium

## Problem
Agents start with zero project knowledge every time. Mozilla cq: "Stack Overflow for agents."

## Design
Agents file lessons on completion (tagged, with confidence). Stored in .sdd/memory/lessons.jsonl. New agents receive relevant lessons by tag overlap. Lessons decay over time.
