Name:           bernstein
Version:        1.4.11
Release:        1%{?dist}
Summary:        Multi-agent orchestration for AI coding agents
License:        Apache-2.0
URL:            https://github.com/chernistry/bernstein
BuildArch:      noarch
Requires:       python3 >= 3.12

%description
Orchestrate parallel AI coding agents. Runs Claude Code, Codex, Gemini CLI
and others in parallel with git worktree isolation and quality gates.

%install
mkdir -p %{buildroot}%{_bindir}
cat > %{buildroot}%{_bindir}/bernstein << 'WRAPPER'
#!/bin/bash
if command -v pipx &>/dev/null; then
    exec pipx run bernstein "$@"
elif command -v uvx &>/dev/null; then
    exec uvx bernstein "$@"
else
    exec python3 -m pip install --user bernstein &>/dev/null && exec python3 -m bernstein "$@"
fi
WRAPPER
chmod 755 %{buildroot}%{_bindir}/bernstein

%files
%{_bindir}/bernstein

%changelog
* Thu Apr 03 2026 Alex Chernysh <alex@alexchernysh.com> - 1.4.11-1
- Switch to wrapper RPM: installs via pipx/uvx instead of native Python RPM
- Fixes COPR build failures from missing Fedora packages for Python deps
