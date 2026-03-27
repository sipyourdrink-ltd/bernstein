# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability, please report it responsibly:

**Email:** [alex@alexchernysh.com](mailto:alex@alexchernysh.com)

Please do **not** open a public issue for security vulnerabilities.

## Scope

Bernstein orchestrates AI coding agents that execute code on your machine. By design:

- Agents have access to your local filesystem and CLI tools
- The task server runs on `localhost:8052` (not exposed externally)
- Evolution only modifies data files (prompts, configs) — never Python source

## Supported Versions

| Version | Supported |
|---------|-----------|
| 0.1.x   | Yes       |
