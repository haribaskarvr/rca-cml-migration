import { SfCommand } from '@salesforce/sf-plugins-core';
import { Messages } from '@salesforce/core';
import { commonFlags, executeMigration, importFlags, type MigrationOutput } from '../../utils/migration.js';

Messages.importMessagesDirectoryFromMetaUrl(import.meta.url);
const messages = Messages.loadMessages('rca-cml-migration', 'cml.import');

export default class CmlImport extends SfCommand<MigrationOutput> {
  public static readonly summary = messages.getMessage('summary');
  public static readonly description = messages.getMessage('description');
  public static readonly examples = messages.getMessages('examples');

  public static readonly flags = {
    ...commonFlags(),
    ...importFlags(),
  };

  public async run(): Promise<MigrationOutput> {
    const { flags } = await this.parse(CmlImport);
    return executeMigration('import', flags, this.jsonEnabled(), (message, exitCode) =>
      this.error(message, { exit: exitCode })
    );
  }
}
