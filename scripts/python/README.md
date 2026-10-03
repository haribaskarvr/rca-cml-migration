# CML Org Migration

Use [migrate_cml.py](migrate_cml.py) to export a Revenue Cloud constraint model and import it into another org. Requires Python 3.10+ and authenticated Salesforce CLI aliases. Uses only the Python standard library.

Run the commands below from the project root.

The script transfers plain UTF-8 CML from `ExpressionSetDefinitionVersion.ConstraintModel`, its `ExpressionSetConstraintObj` Type/Port associations, and its context binding. Catalog records and the context definition must already exist in the target. It does not convert BRE rules, migrate catalog/context metadata, clone version history, activate models, or delete associations. Linked models and any other external dependencies must be migrated separately.

## Execution Order

The CLI and orchestration functions are near the top of the script. Supporting functions follow, with target preflight, apply, and readback verification separated into distinct phases:

1. Parse and validate options, including paired update/version flags and JWT settings.
2. Create the run folder. For import, validate the copied export and mapping JSON before authentication.
3. Authenticate the required orgs and establish read-only API clients.
4. Export the selected source model/version when requested.
5. Validate catalog mappings, target version/context, associations, and permissions; write the preflight report.
6. Stop for dry-run. For apply, check the production gate, save the backup/journal, and enable writes only within the apply phase.
7. Recheck target state, perform the planned changes, verify CML/association/context readback, and disable writes again on success or failure.

Manifest validation checks UTF-8 CML, checksum, JSON shape, resource identities, and typed catalog references. Mapping entries must identify exported resources of the specified object type. Invalid version selections report the available definition-version IDs/numbers. No failed write is automatically retried.

## Export and Dry Run

Replace `SOURCE`, `TARGET`, and `GB_MODEL` with your authenticated org aliases and the parent constraint model's `ExpressionSet.ApiName`, not a version API name:

```powershell
python scripts/python/migrate_cml.py migrate --source-org SOURCE --target-org TARGET --model-api GB_MODEL --output-dir migration/GB_MODEL --dry-run
```

Import and migrate default to dry-run even when `--dry-run` is omitted. Dry-run reads both orgs and writes local export/report files, but makes **no target-org writes**. `--output-dir` is a parent directory: every invocation automatically creates a unique timestamped `run-*` subfolder and prints its full path before authentication. Reuse the same output directory on subsequent runs; previous artifacts are never overwritten.

Run folders include the operation and mode before the UTC timestamp and unique suffix:

- `run-export-only-*`: source export without target import.
- `run-migrate-dryrun-*`: source export plus target preflight, with no target writes.
- `run-import-dryrun-*`: preflight of an existing export, with no target writes.
- `run-migrate-apply-*`: source export plus an explicitly requested target import.
- `run-import-apply-*`: an explicitly requested target import from an existing export.

The label identifies the requested operation/mode, not its success. Check terminal output and, for an apply run, the action journal. Existing folders remain valid import inputs and are not renamed.

If multiple source definition versions exist, specify `--source-version` with the exact `ExpressionSetDefinitionVersion` ID, or a version number when exposed by the org schema. An `ExpressionSetVersion` ID is not accepted. The script stops instead of choosing an arbitrary version. Use `--api-version 67.0`, for example, to select a version supported by both orgs; otherwise each connection uses its latest available version.

To export without connecting to a target:

```powershell
python scripts/python/migrate_cml.py export --source-org SOURCE --model-api GB_MODEL --source-version SOURCE_DEFINITION_VERSION_ID --output-dir migration/GB_MODEL
```

Artifacts inside each run folder are `model.cml`, `manifest.json`, and, after target preflight, `preflight.json`. Any supplied mapping file is copied as `mappings.json`. The manifest contains logical catalog identities, source record IDs for provenance/mapping, and a CML checksum. It contains no access token. Keep the CML and manifest together; editing the CML requires a fresh export because the checksum is verified before import.

## Import

Review the preflight report, then explicitly apply the existing export. Replace `RUN_FOLDER` below with the `run-*` folder printed by the export/migrate command:

```powershell
python scripts/python/migrate_cml.py import --target-org TARGET --input-dir migration/GB_MODEL/RUN_FOLDER --apply
```

Import creates a new sibling run folder, copies the selected CML and manifest into it, and writes that invocation's report and backups there. Use import's optional `--output-dir` to choose a different parent. The original export remains unchanged. Existing legacy export directories containing `model.cml` and `manifest.json` can still be used as `--input-dir`.

The target model name and context developer name default to the exported values. Override them with `--target-model-api` and `--context-definition` when needed. Import rejects source and target aliases that resolve to the same org.

New models are the default. Updating an existing target requires both `--update-existing` and `--target-version`, selecting a verified Draft/Inactive definition version with no active linked runtime version:

```powershell
python scripts/python/migrate_cml.py import --target-org TARGET --input-dir migration/GB_MODEL/RUN_FOLDER --update-existing --target-version TARGET_DEFINITION_VERSION_ID --dry-run
python scripts/python/migrate_cml.py import --target-org TARGET --input-dir migration/GB_MODEL/RUN_FOLDER --update-existing --target-version TARGET_DEFINITION_VERSION_ID --apply
```

Production writes additionally require `--allow-production`. The script never activates or deactivates a model. If context-link creation is not supported by the target schema/permissions, configure the target model and context in Constraint Builder first, then import with the explicit update/version options.

## Catalog Matching

- Products: exact, nonempty `Product2.External_Id__c` values. Field spelling is resolved through org describe (the existing loader uses `External_ID__c`). Use `--product-key` during export to select another stable product key. No implicit product-name fallback.
- Classifications and relationship types: exact unique names, or explicit record mappings.
- Component groups: mapped owning product, group name, and complete parent-group ancestry.
- Bundle components: mapped parent, child product/classification, component group, and relationship type where present. Multiple matches require an explicit mapping; the script does not guess.

For ambiguity or intentional classification/relationship renames, provide `--mapping-file path/to/mappings.json`. Its format is object API name -> exported source record ID -> target record ID (replace the placeholders with real IDs):

```json
{
  "ProductClassification": {
    "SOURCE_CLASSIFICATION_ID": "TARGET_CLASSIFICATION_ID"
  },
  "ProductRelatedComponent": {
    "SOURCE_COMPONENT_ID": "TARGET_COMPONENT_ID"
  }
}
```

Explicit product/group/component mappings must still match the exported logical identity. Missing dependencies, invalid tags, duplicate associations, stale target associations, incompatible context bindings, and catalog references owned by another model block import before org writes.

## Progress and Authentication

Terminal logs include timestamps, phase labels, association/reference counts, and elapsed time. Messages flush immediately. `--auth-mode sf-cli` is the default and reuses existing authenticated `sf` aliases, so existing IDE commands remain unchanged. `sf org display --json` supplies the instance URL and org ID, and `sf org auth show-access-token --json` supplies the access token without an interactive prompt. Both commands' output is captured privately; access tokens are held in memory and never printed or written to migration artifacts. Your Salesforce CLI must support `org auth show-access-token`. No temporary secret-display environment setting is required.

### JWT Pipeline Authentication

Use `--auth-mode jwt` to run `sf org login jwt` before connecting to the selected orgs. The aliases passed through `--source-org` and `--target-org` are created or refreshed; the CLI default org is not changed. Source and target aliases must be distinct.

Supply the following environment variables through Azure DevOps secret variables/Secure Files or GitHub Actions secrets and protected key files. Do not put private-key contents or access tokens in command-line arguments, the repository, or pipeline artifacts.

| Setting | Source Org Variable | Target Org Variable |
| --- | --- | --- |
| Approved deployment username | `SF_SOURCE_USERNAME` | `SF_TARGET_USERNAME` |
| OAuth client ID | `SF_SOURCE_CLIENT_ID` | `SF_TARGET_CLIENT_ID` |
| Readable private-key file path | `SF_SOURCE_JWT_KEY_FILE` | `SF_TARGET_JWT_KEY_FILE` |
| HTTPS login or My Domain URL | `SF_SOURCE_INSTANCE_URL` | `SF_TARGET_INSTANCE_URL` |

Each applicable setting is required. For a sandbox, use its My Domain URL or `https://test.salesforce.com`; for production, use its My Domain URL or `https://login.salesforce.com`. Configure the OAuth client's certificate and approve the deployment user for JWT login beforehand. The pipeline must provision and protect the private-key files and remove them after use.

- `export` requires only the source variables and authenticates only the source org.
- `import` requires only the target variables and authenticates only the target org.
- `migrate` requires both sets. Both sets are validated before any JWT login is attempted.

Example pipeline validation command:

```powershell
python scripts/python/migrate_cml.py migrate --auth-mode jwt --source-org CI_SOURCE --target-org CI_TARGET --model-api GB_CML --output-dir migration/GB_CML --dry-run
```

Dry-run still performs authentication, which can create or refresh the Salesforce CLI's local auth cache and aliases, but makes no target configuration writes. Login output is captured rather than printed; logs report only the role and alias. JWT mode does not fall back to existing authentication if its settings or login fail. Prefer ephemeral pipeline agents, keep CLI auth files out of artifacts, and retain the normal approval gate before `--apply`.

## Pipeline Setup

Use separate source-export and target-deploy stages where practical. This keeps source credentials out of target jobs and lets reviewers approve the exact export that was checked. The same commands work in Azure DevOps and GitHub Actions; map the secret, secure-file, artifact, and approval steps to the platform's native features.

1. **Prepare the Salesforce connection.** Configure a connected app for JWT bearer flow, upload its certificate, and authorize a dedicated deployment user in each org. Grant only the access needed to read the source model/catalog and to create or update the target model. Use sandbox/My Domain URLs as appropriate.
2. **Prepare the runner.** Use an ephemeral Windows or Linux agent with Python 3.10+ and Salesforce CLI v2 installed. Make `migrate_cml.py` available from the checked-out repository. Do not add its directory or the CLI auth cache to published artifacts.
3. **Configure secrets and key files.** Store the four `SF_SOURCE_*` and/or `SF_TARGET_*` settings from the JWT table as protected pipeline variables. Store each private key using Azure Secure Files or an equivalent protected file facility. Set `SF_*_JWT_KEY_FILE` to the downloaded file path for that job, restrict access, and delete the file in an unconditional cleanup step. Never echo secret variables or key contents.
4. **Pin the migration inputs.** Set the source and target aliases, model API name, source definition-version ID, and artifact parent directory in pipeline parameters or reviewed configuration. Keep source and target aliases distinct. Pin `--source-version` so a new source version cannot silently change the payload during a later run.
5. **Export and publish once.** In the source stage, run:

  ```powershell
  python scripts/python/migrate_cml.py export --auth-mode jwt --source-org CI_SOURCE --model-api GB_CML --source-version SOURCE_DEFINITION_VERSION_ID --output-dir migration/GB_CML
  ```

  Capture the printed `run-export-only-*` folder and publish that exact folder, containing `model.cml` and `manifest.json`, as an immutable pipeline artifact. Do not publish the whole workspace or CLI authentication files.
6. **Download and preflight on the target.** In a target stage, download that artifact and run a dry-run against its exact folder:

  ```powershell
  python scripts/python/migrate_cml.py import --auth-mode jwt --target-org CI_TARGET --input-dir migration/GB_CML/RUN_FOLDER --dry-run
  ```

  Retain the printed run-folder path and `preflight.json` as stage output. A failed preflight must fail the stage; fix the dependency or mapping and produce a new reviewed export if the source artifact needs to change.
7. **Gate and apply the same artifact.** Add the platform's protected-environment/manual approval between preflight and apply. After approval, download the same immutable export artifact and run:

  ```powershell
  python scripts/python/migrate_cml.py import --auth-mode jwt --target-org CI_TARGET --input-dir migration/GB_CML/RUN_FOLDER --apply
  ```

  Add `--allow-production` only in a separately protected production stage. The script runs preflight again immediately before writes; approval does not bypass that check.
8. **Retain results and serialize deployments.** Always publish the import run folder, preflight report, target backup, and action journal as restricted artifacts, including on failure. Retain terminal logs without secrets. Prevent concurrent applies to the same target/model, and do not automatically retry an apply that may have partially completed; inspect its journal and target state first.

The single-command `migrate` operation is also suitable for a non-production validation job that authenticates both orgs and exports/preflights in one run:

```powershell
python scripts/python/migrate_cml.py migrate --auth-mode jwt --source-org CI_SOURCE --target-org CI_TARGET --model-api GB_CML --source-version SOURCE_DEFINITION_VERSION_ID --output-dir migration/GB_CML --dry-run
```

For a gated release, prefer the staged export/import flow above so the target apply consumes the exact artifact that passed preflight. The script does not activate the model; perform supported compilation, configurator validation, and activation as separate reviewed steps.

## Verification and Recovery

Before applying, the script creates a timestamped `target-backup-*` directory inside the current run folder with the target snapshot, existing CML when updating, and an action journal. Each successful creation/upload is recorded. It verifies target CML bytes, associations, context binding, and inactive version state after import. An identical existing target with explicit update/version flags requires no writes.

Salesforce API calls are not one cross-request transaction. A failed apply can leave partial changes, including a newly created model. No automatic rollback or destructive cleanup is attempted. Review the journal and snapshot before retrying; a newly created partial model must be addressed as an existing model with an explicit inactive target version. Network-failed writes may have succeeded server-side even if no response was received, so inspect the target as well as the journal. Backups contain business configuration: protect them accordingly.

Ctrl+C returns exit code 130. If interrupted during apply, the journal is marked as interrupted and the write guard is reset. A request already sent to Salesforce may still have completed, so inspect the target before retrying. Invalid options and handled failures return nonzero; successful completion returns zero.

After a successful import, validate and activate through the supported Salesforce workflow and exercise the product configurator. Local syntax/CLI checks do not establish runtime compatibility with your org release.