# Private OFX release workflows

The public Action repository is a runner only. Its workflows check out OFX
source from the private `MoazElgabry/OFX-Building` repository, build Windows and
macOS in parallel, upload each completed package directly to a WordPress draft,
and complete the draft after both uploads succeed. They do not build a Linux
OFX plugin or save build artifacts in GitHub Actions. They never publish the
WordPress draft automatically.

Run **Build Chromonotgen** or **Build B-Grade** from Actions and select the
private source ref. They use numeric versions `0.4.0` and `0.1.0` on the
`private` release channel. The WordPress project must already be registered
with licensed access and its approved users must have an active grant for the
project slug. The existing Plugin Manager **Development builds** preference
controls visibility for both `dev` and `private` releases.

## Repository configuration

- Secret `OFX_BUILDING_READ_TOKEN`: read-only checkout access to the private
  source repository.
- Secret `MOAZ_ER_USERNAME`: WordPress automation account username.
- Secret `MOAZ_ER_APPLICATION_PASSWORD`: that account's Application Password.
- Variable `MOAZ_ER_ACCESS_PUBLIC_KEY_BASE64URL`: the public key embedded in
  local and CI binaries. It is public verification material, not a secret.

Do not store the signing seed, an invitation key, or any user's private
credential in this repository or its Actions settings.

## Publishing behavior

The workflow creates a private WordPress draft before compiling; this also
checks WordPress authentication before spending time on the builds. Each
platform job uploads its own package directly to that draft. Only after both
jobs finish does the final job complete draft metadata and report missing
WordPress targets. If a build or upload fails, the draft remains incomplete
and unpublished for administrator review.

The composite action remains reusable by existing development workflows. Its
`channel` input defaults to `dev`; private project workflows pass `private`.
