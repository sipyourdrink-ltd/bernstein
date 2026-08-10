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

  # BROKEN as-is: `virtualenv_install_with_resources` with no `resource`
  # blocks installs bernstein via `pip --no-deps`, so the resulting binary
  # fails at startup (ModuleNotFoundError: no module named 'click').
  # Do not publish this formula until the runtime closure ships as
  # resources (or the install strategy changes). Tracked in
  # https://github.com/sipyourdrink-ltd/bernstein/issues/3573.
  def install
    virtualenv_install_with_resources
  end

  test do
    assert_match "bernstein", shell_output("#{bin}/bernstein --version")
  end
end
