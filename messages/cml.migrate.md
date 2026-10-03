# summary

Export and preflight or import a Revenue Cloud CML model.

# description

Runs source export followed by target preflight. Target writes require --apply; dry-run is the default. `--source-version` accepts the `ExpressionSetDefinitionVersion` Salesforce ID (15 or 18 characters) or its exposed version number; provide it when multiple versions exist.

# examples

- <%= config.bin %> <%= command.id %> --source-org SOURCE_ORG_ALIAS --target-org TARGET_ORG_ALIAS --model-api CML_API_NAME --source-version SOURCE_DEFINITION_VERSION_ID_OR_NUMBER --output-dir migration/CML_API_NAME --dry-run
