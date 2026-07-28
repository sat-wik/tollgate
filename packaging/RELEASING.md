# Releasing

Two one-time setups, then every release is three commands.

## One-time: PyPI Trusted Publishing

No API token is involved — PyPI verifies the release came from this repo's
Actions workflow.

1. Sign in at [pypi.org](https://pypi.org) and enable 2FA (required to upload).
2. Go to **Your projects → Publishing** — or, before the first release,
   [Add a pending publisher](https://pypi.org/manage/account/publishing/) — and
   register:

   | Field | Value |
   |---|---|
   | PyPI project name | `tollgate-proxy` |
   | Owner | `sat-wik` |
   | Repository | `tollgate` |
   | Workflow name | `release.yml` |
   | Environment | `pypi` |

3. In GitHub: **Settings → Environments → New environment → `pypi`**. Adding a
   required reviewer here means a release waits for your approval before it
   uploads, which is worth it for something that can't be undone.

`tollgate-proxy` is unclaimed; the name is yours on first upload. Note that a
version number can never be reused, even after deleting a release — rehearse
against [TestPyPI](https://test.pypi.org) if you want to.

## One-time: the Homebrew tap

A tap needs no approval from Homebrew — it's just a repo with a known name.

1. Create a public GitHub repo called **`homebrew-tollgate`** under `sat-wik`.
2. Copy `Formula/tollgate.rb` (generated below) into it at the same path.

People then install with:

```sh
brew install sat-wik/tollgate/tollgate
```

Getting into `homebrew-core`, so that plain `brew install tollgate` works, needs
roughly 30 forks / 30 watchers / 75 stars and a reviewed pull request. The tap
works today and needs none of that.

## Every release

```sh
# 1. Bump the version in pyproject.toml, then:
git tag v0.3.0 && git push origin v0.3.0
```

The `release` workflow runs the tests, checks the tag matches
`pyproject.toml`, builds, and publishes to PyPI.

```sh
# 2. Regenerate the formula against the release that now exists
python packaging/generate_formula.py 0.3.0 > Formula/tollgate.rb

# 3. Copy it into the tap repo and push
```

The generator resolves the dependency tree with pip and reads every sdist hash
from PyPI, so the formula always matches a real install. It reads dependencies
from `pyproject.toml`, so adding one is picked up automatically.

If a future dependency needs a compiler, the generator adds the matching
`depends_on ... => :build` line. Today the tree is nine pure-Python packages and
needs none — worth keeping that way, since a build toolchain turns a
ten-second `brew install` into a several-minute one.
