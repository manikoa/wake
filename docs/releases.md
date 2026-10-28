# Releases

Decided October 27, 2026.

Wake records evidence other people rely on, so its own releases have to be checkable by anyone, without trusting the people who cut them. Every release is built by `.github/workflows/release.yml` from a tag, signed by that workflow's identity rather than by anyone's key, and ships with provenance that says what built it, from which commit.

## What a release ships

Pushing a tag `vX.Y.Z` releases:
- **Images:** `ghcr.io/manikoa/wake-collector:X.Y.Z`, which also holds `wake-keygen` and `wake-log`, and `ghcr.io/manikoa/wake-proxy:X.Y.Z`. linux/amd64 for now.
- **`wake-log` and `wake-keygen` on their own**, so anyone can check a log without running Wake: `wake-log_X.Y.Z_<os>_<arch>.tar.gz` for Linux and macOS, amd64 and arm64. There's no Windows build: Tessera's log storage locks files the Unix way.
- **The Python SDK**, `wake-sdk` X.Y.Z, on PyPI. The same wheel and sdist are attached to the GitHub release.
- **The TypeScript SDK**, `wake-sdk` X.Y.Z, on npm. The same tarball, `wake-sdk-X.Y.Z.tgz`, is attached to the GitHub release. It carries the repository's `LICENSE` and `NOTICE`, which packing copies in.

Each artifact gets:
- **A Sigstore signature**, keyless. The signing certificate names the workflow and the tag (`https://github.com/manikoa/wake/.github/workflows/release.yml@refs/tags/vX.Y.Z`), and the signature is in Sigstore's public transparency log. No one holds a release key that could leak.
- **SLSA build provenance**, as a GitHub artifact attestation: the commit, the workflow and the runner that built it. An image's attestations are also pushed to the registry beside it.
- **An SBOM** in SPDX, attached to the release and attested the same way.

The GitHub release lists every file's SHA-256 in `SHA256SUMS`, signed in `SHA256SUMS.sigstore.json`. PyPI and npm get the SDKs by trusted publishing, so there's no PyPI or npm token either. Each keeps its own attestations, which it shows on the package's page. npm publishes the tarball the release attested, as it is.

The inputs are pinned: actions to commit SHAs, base images by digest, and agentgateway in `deploy/compose.yaml` by digest. Dependabot (`.github/dependabot.yml`) opens a pull request each week for any that moved.

The version names the release: both SDKs and the proxy carry it, and the images and the `wake-log` builds are named for it. The `log/` Go module keeps its own `log/vX.Y.Z` tags for its Go API, which `wake-platform` imports, and those tags don't run the release.

## Cutting a release

1. Set the version in `sdk/python/pyproject.toml`, `sdk/typescript/package.json` and `proxy/Cargo.toml`, then update the lock files: `uv lock` in `sdk/python/` and `cargo update --workspace` in `proxy/`. CI and the proxy's image build use them `--locked`.
2. Merge that to main.
3. Tag the merge commit and push the tag:

   ```bash
   git tag v0.1.0 origin/main
   git push origin v0.1.0
   ```

4. The workflow first checks that the tag matches every version, then builds and signs everything and creates the release. PyPI and npm come last, each from its own environment (`pypi`, `npm`), so a problem with either never holds up the rest.

If a job fails, re-run the failed jobs. A tag is never moved or reused: fix forward with the next patch version.

## Checking a release

You need [cosign](https://docs.sigstore.dev/cosign/system_config/installation/) and the [GitHub CLI](https://cli.github.com/). Every check below pins the identity to this repository's release workflow, so a signature made by any other workflow, or by a fork, fails.

**Images.** Check the signature and the provenance, then deploy by the digest `cosign verify` printed:

```bash
cosign verify ghcr.io/manikoa/wake-collector:0.1.0 \
  --certificate-identity https://github.com/manikoa/wake/.github/workflows/release.yml@refs/tags/v0.1.0 \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
gh attestation verify oci://ghcr.io/manikoa/wake-collector:0.1.0 \
  --repo manikoa/wake --signer-workflow manikoa/wake/.github/workflows/release.yml
```

The same goes for `wake-proxy`. Add `--predicate-type https://spdx.dev/Document/v2.3` to `gh attestation verify` to check the SBOM instead.

**`wake-log` builds.** Check the signed checksums, then the file's own provenance:

```bash
gh release download v0.1.0 -R manikoa/wake -p 'wake-log_0.1.0_linux_amd64.tar.gz' -p 'SHA256SUMS*'
cosign verify-blob SHA256SUMS --bundle SHA256SUMS.sigstore.json \
  --certificate-identity https://github.com/manikoa/wake/.github/workflows/release.yml@refs/tags/v0.1.0 \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
sha256sum --check --ignore-missing SHA256SUMS
gh attestation verify wake-log_0.1.0_linux_amd64.tar.gz \
  --repo manikoa/wake --signer-workflow manikoa/wake/.github/workflows/release.yml
```

On macOS, `shasum -a 256 --check --ignore-missing SHA256SUMS`.

**The SDKs.** The wheel on PyPI is the one the workflow built and attested:

```bash
pip download wake-sdk==0.1.0 --no-deps -d wake-sdk
gh attestation verify wake-sdk/wake_sdk-0.1.0-py3-none-any.whl \
  --repo manikoa/wake --signer-workflow manikoa/wake/.github/workflows/release.yml
```

So is the tarball on npm:

```bash
npm pack wake-sdk@0.1.0
gh attestation verify wake-sdk-0.1.0.tgz \
  --repo manikoa/wake --signer-workflow manikoa/wake/.github/workflows/release.yml
```

In a project that installed it, `npm audit signatures` also checks npm's own provenance for it.

## Set up once

These live in GitHub's, PyPI's and npm's settings, not in the repository:
- **PyPI:** add a trusted publisher for `wake-sdk` (a pending one, until the first release creates the project): owner `manikoa`, repository `wake`, workflow `release.yml`, environment `pypi`.
- **The `pypi` environment** in this repository's settings, limited to `v*` tags. A required reviewer makes each PyPI upload wait for a person.
- **npm:** add a trusted publisher for `wake-sdk`: GitHub Actions, owner `manikoa`, repository `wake`, workflow `release.yml`, environment `npm`. npm sets one on a package that already exists, so check its trusted publishing docs on whether the first version has to be published by hand. After that, require two-factor authentication and disallow tokens, so only the workflow publishes.
- **The `npm` environment**, limited to `v*` tags, with a required reviewer like `pypi`'s.
- **A tag ruleset** so only maintainers can create `v*` tags, and none can be moved or deleted. **Immutable releases** keep a published release's files from being swapped.
- **The packages:** after the first release, make `wake-collector` and `wake-proxy` public in their package settings so they pull without signing in.
