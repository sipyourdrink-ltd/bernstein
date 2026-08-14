# Both version fields are bound to the release tag by
# scripts/build_copr_srpm.py before the SRPM is built; the committed values are
# only what makes this file buildable on its own.
#
# `Version:` is the RPM spelling (`~` separates a pre-release), %%pypi_version
# is the spelling PyPI uses (`-`). They differ only for pre-release tags, and
# the package must be fetched under the name PyPI knows.
%global pypi_version 1.4.11

# The venv's bootstrap pip comes from the chroot and ranges from 23.2 (EPEL 9)
# to current (Fedora). Pin the one that resolves the closure so a rebuild of
# the same SRPM does not change resolver behaviour with whatever pip happens to
# be newest that day.
%global pip_version 26.2.1

# The application lives in a private virtualenv rather than in the system
# site-packages: its dependency closure is ~80 packages, most of which Fedora
# does not carry, and none of which we want to force onto a user's system
# interpreter.
%global venv_root %{_libdir}/%{name}

# RHEL/EPEL 9 ships 3.9 as `python3`, which is below the `requires-python`
# floor in pyproject.toml. The interpreter we need is parallel-installable from
# AppStream under a versioned name. Every other chroot we publish to already
# has `python3` >= 3.12, so it needs no special case.
%if 0%{?rhel} == 9
%global python_pkg python3.12
%global python_bin %{_bindir}/python3.12
%else
%global python_pkg python3
%global python_bin %{_bindir}/python3
%endif

Name:           bernstein
Version:        1.4.11
# Release 2, not 1: the previous package was `noarch` and carried only a
# launcher script. RPM will not replace an installed package with one of the
# same name-version-release, so a host that already has the launcher at this
# version would keep it. Bumping the release makes the change an unambiguous
# upgrade on every host, whatever it has installed.
Release:        2%{?dist}
Summary:        Deterministic orchestrator for CLI coding agents
License:        Apache-2.0
URL:            https://github.com/sipyourdrink-ltd/bernstein

# Not noarch. The dependency closure carries compiled extension modules
# (cryptography, pillow, lxml, pydantic-core, grpcio), so the built package is
# specific to both the architecture and the interpreter ABI of its chroot.
BuildRequires:  %{python_pkg}
# A C++ toolchain, because not every chroot has a prebuilt wheel for every
# dependency. On the released Fedora and EPEL chroots the closure resolves to
# manylinux wheels and nothing compiles; on a chroot whose interpreter is ahead
# of the wheel publishers - rawhide is permanently in that state - pip falls
# back to building a sdist and the build dies on a missing `g++`.
BuildRequires:  gcc-c++
Requires:       %{python_pkg}

# The venv is a self-contained tree. The manylinux wheels inside it carry their
# own vendored shared objects with mangled sonames (libjpeg-f7df23c0.so.62,
# liblzma-d6711707.so.5, ...); rpm's automatic scanner turns those into
# Requires that nothing on the system can ever provide, which makes the package
# uninstallable. Excluding the tree from both generators is what keeps the
# dependency set equal to the one line above.
%global __requires_exclude_from ^%{venv_root}/.*$
%global __provides_exclude_from ^%{venv_root}/.*$
# Prebuilt wheels bring their own stripped binaries and their own .pyc policy;
# rpm's debuginfo, shebang-mangling and bytecompile passes have nothing to
# contribute and fail on paths inside the venv.
%global debug_package %{nil}
%global __brp_mangle_shebangs %{nil}
%global __brp_python_bytecompile %{nil}

%description
Deterministic orchestrator for CLI coding agents. Runs Claude Code, Codex,
Gemini CLI and 40+ others in parallel with per-task git worktree isolation
and quality gates. No model sits in the coordination loop, so the same plan
replays to a byte-identical task graph.

This package ships the application and its entire dependency closure in a
private virtualenv under %{_libdir}/%{name}. It resolves nothing at run time,
so the installed version is the version named in the package metadata and the
command works without network access.

%install
mkdir -p %{buildroot}%{venv_root}
%{python_bin} -m venv %{buildroot}%{venv_root}

# The dependency closure is resolved once, here, at the exact version this
# package claims to be. Nothing is fetched after this point - not on first
# run, not ever.
%{buildroot}%{venv_root}/bin/python -m pip install \
    --no-cache-dir --disable-pip-version-check "pip==%{pip_version}"
%{buildroot}%{venv_root}/bin/python -m pip install \
    --no-cache-dir --disable-pip-version-check "%{name}==%{pypi_version}"

# A venv records the interpreter path it was created with in console-script
# shebangs and in a few pip bookkeeping files. Built under the buildroot, those
# point at a directory that will not exist on the installed system; rpm's own
# check-buildroot would fail the build on them. Text files only - `grep -I`
# skips binaries so no wheel payload is rewritten.
grep -rIl "%{buildroot}" %{buildroot}%{venv_root} \
    | xargs -r sed -i "s|%{buildroot}||g"

# `bernstein` on PATH is a symlink to the venv's console script, so the
# interpreter that runs is always the private one.
mkdir -p %{buildroot}%{_bindir}
ln -sf %{venv_root}/bin/%{name} %{buildroot}%{_bindir}/%{name}

%check
# The package must run the version it claims. pip resolving something else -
# a stale index, a yanked release, a typo in the binding above - fails the
# build here rather than shipping a package whose metadata is fiction.
#
# Compared as PEP 440 versions rather than as strings. A pre-release reaches
# this spec in the spelling its tag used (3.15.0-rc1) while the installed
# distribution metadata carries the normalised spelling (3.15.0rc1), so a
# string comparison would fail every pre-release build even though the right
# version is installed. `packaging` is one of the application's own runtime
# dependencies, so it is always present in the venv being checked.
%{buildroot}%{venv_root}/bin/python -c \
    'import importlib.metadata as m, sys; \
     from packaging.version import Version; \
     got = m.version("%{name}"); \
     sys.exit(0) if Version(got) == Version("%{pypi_version}") else sys.exit( \
         "packaged %s but the spec claims %{pypi_version}" % got)'

%files
%{venv_root}
%{_bindir}/%{name}

%changelog
* Mon Aug 10 2026 Alex Chernysh <alex@alexchernysh.com> - 1.4.11-2
- Package the application itself: the release and its dependency closure are
  installed into a private virtualenv at build time instead of being resolved
  from PyPI on first run
- Drop noarch: the closure carries compiled extension modules
- Require python3.12 on EPEL 9, where the distribution python3 is 3.9

* Fri Apr 03 2026 Alex Chernysh <alex@alexchernysh.com> - 1.4.11-1
- Switch to wrapper RPM: installs via pipx/uvx instead of native Python RPM
- Fixes COPR build failures from missing Fedora packages for Python deps
