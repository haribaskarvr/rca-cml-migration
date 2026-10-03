# summary

Export and preflight or import a Revenue Cloud CML model.

# description

Runs source export followed by target preflight. Target writes require --apply; dry-run is the default.

# examples

- <%= config.bin %> <%= command.id %> --source-org GB_TEST --target-org GB_PREPROD --model-api GB_CML --source-version DEFINITION_VERSION_ID --output-dir migration/GB_CML --dry-run