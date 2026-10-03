# summary

Export a Revenue Cloud CML model and its associations.

# description

Writes the selected definition version as UTF-8 CML with a checksummed manifest in a unique local run folder.

# examples

- <%= config.bin %> <%= command.id %> --source-org GB_TEST --model-api GB_CML --source-version DEFINITION_VERSION_ID --output-dir migration/GB_CML