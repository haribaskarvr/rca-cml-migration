# RCA CML Migration

A Salesforce CLI plugin for exporting Revenue Cloud Constraint Model Language (CML) from one org and preflighting or importing it into another.

It migrates the CML, its constraint associations, and context binding. The target catalog records and context definition must already exist. It does not convert BRE rules, migrate catalog metadata, or activate models.

## Requirements

- Salesforce CLI v2
- Python 3.10+ (`python3` or `python`; on Windows, `python` or `py -3`)
- Authenticated Salesforce CLI org aliases, or JWT settings for `--auth-mode jwt`

The bundled Python engine uses only the standard library.

## Install

Install Python first if it is not already available:

| Platform         | Install                                           | Verify              |
| ---------------- | ------------------------------------------------- | ------------------- |
| Windows (WinGet) | `winget install --exact --id Python.Python.3.13`  | `py -3 --version`   |
| macOS (Homebrew) | `brew install python`                             | `python3 --version` |
| Ubuntu/Debian    | `sudo apt update` then `sudo apt install python3` | `python3 --version` |

The reported version must be **3.10 or newer**; older Linux releases may provide an older Python. Alternatively, use the [official Python downloads](https://www.python.org/downloads/). On Windows, enable **Add Python to PATH** if offered. Reopen your terminal after installation. No `pip install` step is required.

Then install the plugin into Salesforce CLI:

```powershell
sf plugins install rca-cml-migration
sf cml --help
```

## Quick Start

Export a model. `--model-api` is the parent `ExpressionSet.ApiName`:

```powershell
sf cml export --source-org SOURCE_ORG_ALIAS --model-api CML_API_NAME --source-version SOURCE_DEFINITION_VERSION_ID_OR_NUMBER --output-dir migration/CML_API_NAME
```

`--source-version` accepts the 15- or 18-character `ExpressionSetDefinitionVersion` Salesforce ID, or its exposed version number. It is required when multiple source versions exist; if omitted, export proceeds only when there is one.

Preflight an exported run in the target org. Dry-run is the default:

```powershell
sf cml import --target-org TARGET_ORG_ALIAS --input-dir migration/CML_API_NAME/RUN_FOLDER --dry-run
```

After reviewing the preflight report, apply explicitly:

```powershell
sf cml import --target-org TARGET_ORG_ALIAS --input-dir migration/CML_API_NAME/RUN_FOLDER --apply
```

Or export and preflight both orgs in one invocation:

```powershell
sf cml migrate --source-org SOURCE_ORG_ALIAS --target-org TARGET_ORG_ALIAS --model-api CML_API_NAME --source-version SOURCE_DEFINITION_VERSION_ID_OR_NUMBER --output-dir migration/CML_API_NAME --dry-run
```

## Safety and Authentication

- `import` and `migrate` preflight without target writes unless `--apply` is supplied.
- Production writes also require `--allow-production`.
- Apply creates a target backup and action journal, then verifies the resulting state. A failed apply can leave partial changes; inspect the journal and target before retrying.
- Salesforce CLI alias authentication is the default. Use `--auth-mode jwt` with the documented source/target environment variables for CI.

Each invocation creates a unique timestamped run folder with its export and reports. Run any command with `--help` for more options, including catalog mappings and target-version updates.

## Develop

```powershell
yarn install
yarn build
sf plugins link .
sf cml --help
```

See the [Python migration engine](scripts/python/migrate_cml.py) for implementation details, [SECURITY.md](SECURITY.md) for security reporting, and [LICENSE](LICENSE) for license terms.
