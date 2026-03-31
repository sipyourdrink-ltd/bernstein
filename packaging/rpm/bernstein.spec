Name:           bernstein
Version:        %{version}
Release:        1%{?dist}
Summary:        Declarative agent orchestration for engineering teams
License:        Apache-2.0
URL:            https://github.com/chernistry/bernstein
Source0:        %{pypi_source bernstein}

BuildArch:      noarch
BuildRequires:  python3-devel
BuildRequires:  python3-pip
Requires:       python3 >= 3.12

%description
Bernstein orchestrates multiple AI coding agents (Claude Code, Codex,
Gemini CLI, Cursor) in parallel. One YAML config, deterministic
scheduling, verified output.

%prep
%autosetup -n bernstein-%{version}

%build
%py3_build

%install
%py3_install

%files
%license LICENSE
%doc README.md
%{_bindir}/bernstein
%{python3_sitelib}/bernstein/
%{python3_sitelib}/bernstein-*.egg-info/

%changelog
