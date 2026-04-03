%global pypi_name bernstein
%global pypi_version 1.4.11

Name:           python-%{pypi_name}
Version:        %{pypi_version}
Release:        1%{?dist}
Summary:        Declarative agent orchestration for engineering teams

License:        Apache-2.0
URL:            https://pypi.org/project/%{pypi_name}/
Source0:        %{pypi_source %{pypi_name} %{pypi_version}}

BuildArch:      noarch
BuildRequires:  python3-devel >= 3.12
BuildRequires:  python3-pip
BuildRequires:  python3-setuptools
BuildRequires:  pyproject-rpm-macros

Requires:       python3 >= 3.12

%description
Bernstein is a declarative multi-agent orchestration system for
engineering teams. It spawns short-lived CLI coding agents, coordinates
them via a file-based state directory, and works with any CLI agent
(Claude Code, Codex, Gemini CLI, etc.).

%prep
%autosetup -n %{pypi_name}-%{pypi_version}

%generate_buildrequires
%pyproject_buildrequires

%build
%pyproject_wheel

%install
%pyproject_install
%pyproject_save_files %{pypi_name}

%files -f %{pyproject_files}
%license LICENSE
%doc README.md
%{_bindir}/bernstein
%{_bindir}/bernstein-worker

%changelog
* Thu Apr 03 2026 Alex Chernysh <alex@alexchernysh.com> - 1.4.11-1
- Initial RPM package
