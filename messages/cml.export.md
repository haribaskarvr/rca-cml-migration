# summary

Export a Revenue Cloud CML model and its associations.

# description

Writes the selected definition version as UTF-8 CML with a checksummed manifest in a unique local run folder. `--source-version` accepts the `ExpressionSetDefinitionVersion` Salesforce ID (15 or 18 characters) or its exposed version number; provide it when multiple versions exist.

# examples

- <%= config.bin %> <%= command.id %> --source-org SOURCE_ORG_ALIAS --model-api CML_API_NAME --source-version SOURCE_DEFINITION_VERSION_ID_OR_NUMBER --output-dir migration/CML_API_NAME
