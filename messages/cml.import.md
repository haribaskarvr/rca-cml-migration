# summary

Preflight or import an exported Revenue Cloud CML model.

# description

Validates the selected export and target org. Dry-run is the default and never writes to the target.

# examples

- <%= config.bin %> <%= command.id %> --target-org TARGET_ORG_ALIAS --input-dir migration/CML_API_NAME/RUN_FOLDER --dry-run
- <%= config.bin %> <%= command.id %> --target-org TARGET_ORG_ALIAS --input-dir migration/CML_API_NAME/RUN_FOLDER --apply
