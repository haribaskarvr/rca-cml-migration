# RCA CML Migration Salesforce CLI Plugin

An Oclif plugin for exporting and importing Revenue Cloud Constraint Model Language (CML) models between Salesforce orgs. It wraps the Python migration engine bundled at `scripts/python/migrate_cml.py`.

## Requirements

- Salesforce CLI v2
- Python 3.10 or newer on `PATH` (`python3`, `python`, or Windows `py`)
- Authenticated Salesforce CLI aliases for `--auth-mode sf-cli`, or the role-specific JWT environment variables documented in [scripts/python/README.md](scripts/python/README.md)

The engine uses Python's standard library only. The plugin preserves the engine's dry-run default, explicit `--apply` gate, production opt-in, artifact backups, action journal, and target readback verification.

## Commands

Export one source model:

```powershell
sf cml export --source-org GB_TEST --model-api GB_CML --source-version DEFINITION_VERSION_ID --output-dir migration/GB_CML
```

Preflight an existing export in the target org:

```powershell
sf cml import --target-org GB_PREPROD --input-dir migration/GB_CML/RUN_FOLDER --dry-run
```

Apply after reviewing preflight output:

```powershell
sf cml import --target-org GB_PREPROD --input-dir migration/GB_CML/RUN_FOLDER --apply
```

Export and preflight both orgs in one invocation:

```powershell
sf cml migrate --source-org GB_TEST --target-org GB_PREPROD --model-api GB_CML --source-version DEFINITION_VERSION_ID --output-dir migration/GB_CML --dry-run
```

Commands support the Python engine's matching, version, JWT, mapping, and production flags. Run any command with `--help` for options. Detailed storage, artifact, pipeline, and recovery guidance is in [scripts/python/README.md](scripts/python/README.md).

## Develop and Link

From this directory, install dependencies and compile, then link the plugin:

```powershell
yarn install
yarn build
sf plugins link .
sf cml --help
```

When running from source, the Python engine path is resolved from the installed plugin package, while migration output and input paths are relative to the current working directory.

## NPM Package

The generated package name is `rca-cml-migration`. Confirm its availability immediately before publishing because package names can be claimed at any time. The package is configured for public access.

### Publish

1. Sign in to npm from your terminal. Do not put npm credentials or tokens in the repository or chat:

   ```powershell
   npm login
   npm whoami
   ```

2. From this plugin directory, install the locked dependencies, build, and inspect the tarball contents:

   ```powershell
   yarn install --frozen-lockfile
   yarn build
   npm pack --dry-run
   ```

   Confirm the package contains `lib/`, `oclif.manifest.json`, `messages/`, and `scripts/python/`. The Python engine is bundled, but Python and Salesforce CLI are not.

3. Confirm `name` and `version` in `package.json`, then publish:

   ```powershell
   npm publish --access public
   ```

   Publishing runs the package's `prepack` script. For later releases, increment the package version using Semantic Versioning before publishing again.

If you choose a scoped name such as `@your-org/plugin-rca-cml-migration`, use a scope you control and update the package name passed to `Messages.loadMessages(...)` in each command before rebuilding.

### Install on Another System

Install Salesforce CLI v2 and Python 3.10+ first, then install the published package:

```powershell
sf plugins install rca-cml-migration@1.0.0
sf cml --help
```

Replace the version with the release you published. On first install, the CLI may ask you to trust an unsigned plugin; verify the package and publisher before accepting. For non-interactive CI, add the reviewed package to the CLI plugin trust allowlist using your organization's normal policy. Authenticate using existing Salesforce CLI aliases or configure the JWT environment variables described in [scripts/python/README.md](scripts/python/README.md).
