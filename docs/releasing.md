# Releasing the SDKs

The Python SDK is published to PyPI as `adaptirate` and the Node SDK to npm as `@adaptirate/sdk`,
by the [publish workflow](../.github/workflows/publish.yml), whenever a GitHub release is
published. It uses trusted publishing: each registry trusts this repository's workflow directly,
so no API token is ever created or stored.

## One-time setup

**PyPI**

1. Sign in at pypi.org (enable two-factor authentication; PyPI requires it).
2. Account settings, then *Publishing*, then *Add a new pending publisher*:
   - PyPI project name: `adaptirate`
   - Owner: `Ari-Han-t`
   - Repository name: `AdaptiRate`
   - Workflow name: `publish.yml`
   - Environment name: `pypi`

**npm**

1. Sign in at npmjs.com and create a free organisation named `adaptirate` (this owns the
   `@adaptirate` scope). If the name is taken, rename the package in `sdk-node/package.json`
   and the imports in the examples and docs.
2. The first version of a package must be published by hand, because npm only lets you add a
   trusted publisher to a package that exists. From `sdk-node/`:

   ```bash
   npm login
   npm install && npm run build
   npm publish --access public
   ```

3. On the package's page on npmjs.com, *Settings*, then *Trusted publisher*, choose GitHub
   Actions: owner `Ari-Han-t`, repository `AdaptiRate`, workflow `publish.yml`, environment `npm`.

**GitHub**

Repository *Settings*, then *Environments*: create environments named `pypi` and `npm`. Adding
yourself as a required reviewer on each means nothing is published without one more click.

## Each release

1. Set the same version in `sdk-python/pyproject.toml` and `sdk-node/package.json`.
2. Commit, then publish a GitHub release whose tag is that version with a `v` in front, for
   example `v0.1.1`.
3. The workflow checks the versions match the tag, builds, and publishes to both registries.

A version can be published only once on each registry; a mistake needs a new version number.
To publish to one registry only, run the workflow by hand from the *Actions* tab and pick it.

## The Java SDK

Not published yet. Maven Central requires proving ownership of the group id (`io.adaptirate`
would need that domain; `io.github.ari-han-t` can be verified through GitHub). Until then, install
it locally with `mvn install` in `sdk-java/`.
