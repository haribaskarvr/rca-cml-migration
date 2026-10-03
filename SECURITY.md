## Security

Report suspected vulnerabilities directly to the project maintainers before
public disclosure. Do not disclose an unpatched vulnerability in a public issue
or include credentials, access tokens, private keys, or customer data in a
report. Include the affected version, reproduction steps, and impact when safe
to do so.

## Security Considerations

This Salesforce CLI plugin coordinates reads and writes to Salesforce orgs and
includes a Python migration engine. Review the permissions and security of the
Salesforce CLI, Python runtime, and npm dependencies used in your environment.

- Use dedicated Salesforce users with only the permissions required for the
  migration, and protect CLI authentication data and JWT private keys.
- Imports default to dry-run. Review preflight output before using `--apply`;
  production writes also require `--allow-production`.
- Migration exports, preflight reports, backups, and action journals may contain
  model content, org record IDs, or other sensitive operational data. Store and
  share them as restricted artifacts, and do not publish them or CLI auth files.
- The plugin does not put access tokens in migration artifacts. Treat local
  logs, pipeline output, and the Salesforce CLI auth cache as sensitive anyway.
