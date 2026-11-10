# Release checklist

Publishing is automated. Pushing a `v*` tag runs `.github/workflows/release.yml`:

1. the full CI (`ci.yml`) runs on that commit;
2. the sdist and wheel are built once;
3. the run **waits for your approval** (the `pypi` environment), then publishes
   those files to PyPI through Trusted Publishing (no API token);
4. a **draft** GitHub Release is created with generated notes and the same
   files attached. It stays private until you publish it.

The tag is not compared with the package version: check the version yourself
(below). A version number can be used on PyPI only once.

## One-time setup

- [ ] PyPI account with two-factor authentication.
- [ ] PyPI → *Your account* → *Publishing* → *Add a new pending publisher*:
      project `tunhub`, owner `LiukDiihMieu`, repository `tunhub`, workflow
      `release.yml`, environment `pypi`. A pending publisher does *not*
      reserve the name: until the first upload, someone else could still take
      it, so release soon after adding it.
- [ ] GitHub → repository *Settings* → *Environments* → *New environment* `pypi`
      → *Required reviewers*: yourself. Optionally limit deployments to tags
      matching `v*`.

## Before tagging

- [ ] `main` is clean, up to date, and its CI is green.
- [ ] Version bumped in `tunhub/__init__.py` (`__version__`, the single source;
      `pyproject.toml` reads it).
- [ ] `CHANGELOG.md`: the section for this version has its date instead of
      "unreleased".
- [ ] README install instructions match the release.
- [ ] Local build check:
      `rm -rf dist && uv build && uvx twine check --strict dist/*`
- [ ] The wheel works in a fresh environment, e.g.
      `uv tool install --force ./dist/tunhub-*.whl && tunhub --version`
      (afterwards reinstall your usual version).

## Release

- [ ] `git tag vX.Y.Z && git push origin vX.Y.Z`
- [ ] GitHub → *Actions* → *release*: tests and build pass, then *Review
      deployments* → approve `pypi`.
- [ ] https://pypi.org/project/tunhub/ shows the version; the README renders
      (images, links).
- [ ] GitHub → *Releases*: open the draft, paste the `CHANGELOG.md` section,
      publish.

## After

- [ ] In a fresh environment: `uv tool install tunhub` (or `pipx install
      tunhub`), then `tunhub --version` prints the new version.
- [ ] If a release is broken: fix it and release a new patch version (the
      number cannot be reused); yank the broken one on PyPI if needed.
