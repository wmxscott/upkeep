# Draft formula for wmxscott/homebrew-tap. Fill in url and sha256 once v1.0.0 is tagged:
#   curl -sL https://github.com/wmxscott/upkeep/archive/refs/tags/v1.0.0.tar.gz | shasum -a 256
# Resource versions and hashes come from uv.lock.
class Upkeep < Formula
  include Language::Python::Virtualenv

  desc "Run your update commands in parallel, on demand or on a catch-up schedule"
  homepage "https://github.com/wmxscott/upkeep"
  url "https://github.com/wmxscott/upkeep/archive/refs/tags/v1.0.0.tar.gz"
  sha256 "0000000000000000000000000000000000000000000000000000000000000000"
  license "MIT"
  head "https://github.com/wmxscott/upkeep.git", branch: "main"

  depends_on "python@3.14"

  conflicts_with "up", because: "both install an `up` binary"

  resource "prompt-toolkit" do
    url "https://files.pythonhosted.org/packages/7d/ea/39b988c938f75cb75d7045b5c69f8bfed47ee2152c8837fb403de29d6fb8/prompt_toolkit-3.0.53.tar.gz"
    sha256 "9ec8a0ad96d5c56148b3f914aa79c1564c3fde5d2e6b876e7bc327e353cf8fa6"
  end

  resource "questionary" do
    url "https://files.pythonhosted.org/packages/f6/45/eafb0bba0f9988f6a2520f9ca2df2c82ddfa8d67c95d6625452e97b204a5/questionary-2.1.1.tar.gz"
    sha256 "3d7e980292bb0107abaa79c68dd3eee3c561b83a0f89ae482860b181c8bd412d"
  end

  resource "wcwidth" do
    url "https://files.pythonhosted.org/packages/dc/ac/3a943d2792c9bb368aaa8b50121c0f778460ba2d7fbdc0a0366201d9e761/wcwidth-0.9.1.tar.gz"
    sha256 "5823209b0d43af322ce698c689380d7c15ca31fa8e6e3be8459f27031bef0af5"
  end

  def install
    virtualenv_install_with_resources
  end

  # No service block: `up schedule install` sets up the hourly job itself, so
  # it runs through your login shell and sees the same PATH you do.
  def caveats
    <<~EOS
      Write a starter config, then schedule the tools you mark auto = true:
        upkeep init
        upkeep schedule install
    EOS
  end

  test do
    assert_match "upkeep #{version}", shell_output("#{bin}/up --version")
    assert_match "upkeep #{version}", shell_output("#{bin}/upkeep --version")

    ENV["XDG_CONFIG_HOME"] = testpath/"config"
    ENV["XDG_STATE_HOME"] = testpath/"state"
    (testpath/"config/upkeep/config.toml").write <<~TOML
      [schedule]
      shell = "/bin/sh -c"
      [tools.hello]
      run = "echo hello from upkeep"
    TOML

    assert_match "hello from upkeep", shell_output("#{bin}/upkeep hello")
    assert_match(/hello\s+ok/, shell_output("#{bin}/upkeep status"))
    assert_match "hello from upkeep", shell_output("#{bin}/upkeep log hello")
    shell_output("#{bin}/upkeep nope", 2)
  end
end
