class Bernstein < Formula
  include Language::Python::Virtualenv

  desc "Deterministic orchestrator for CLI coding agents"
  homepage "https://github.com/sipyourdrink-ltd/bernstein"
  # URL and sha256 are auto-updated by CI on each release
  url "https://files.pythonhosted.org/packages/source/b/bernstein/bernstein-VERSION.tar.gz"
  sha256 "PLACEHOLDER"
  license "Apache-2.0"
  head "https://github.com/sipyourdrink-ltd/bernstein.git", branch: "main"

  depends_on "python@3.12"

  # Install strategy: resolve the runtime closure at install time inside the
  # formula's own virtualenv, rather than pinning it as `resource` blocks (#3573).
  #
  # `virtualenv_install_with_resources` was here before and installed nothing but
  # bernstein itself: Homebrew drives it with `pip --no-deps`, and the formula
  # declared no resources, so every install produced a command that died on
  # `import click`. Canonical Homebrew style would be to declare all 81 runtime
  # dependencies as resources. That was tried and rejected: the closure contains
  # four Rust builds (pydantic-core, rpds-py, jiter, cryptography) plus grpcio,
  # lxml and pillow, so sdist-backed resources turn `brew install` into an
  # hour-plus source build needing rust and cmake; and the resources have to be
  # regenerated on every release by `brew update-python-resources`, which is
  # currently broken against modern pip (it sends `--uploaded-prior-to=P1D`,
  # which pip rejects as not an ISO 8601 datetime).
  #
  # So: create the venv, then let pip resolve. `--prefer-binary` keeps the
  # install on wheels wherever one exists, which is the whole closure on both
  # macOS architectures, so nothing compiles in practice.
  #
  # The trade-off is real and worth stating: transitive versions resolve at
  # install time instead of being pinned in the formula, so two installs on
  # different days can differ below the top level. bernstein itself is still
  # pinned and hash-verified -- Homebrew checks `sha256` against the staged
  # sdist, and that sdist is what gets installed.
  # `virtualenv_create` builds the venv with `--without-pip --system-site-packages`,
  # so there is no `libexec/bin/pip` to call: pip is reached as a module through
  # the venv's interpreter.
  def install
    virtualenv_create(libexec, "python3.12")
    system libexec/"bin/python", "-m", "pip", "install", "--prefer-binary", buildpath
    bin.install_symlink libexec/"bin/bernstein"
  end

  test do
    assert_match "bernstein", shell_output("#{bin}/bernstein --version")
  end
end
