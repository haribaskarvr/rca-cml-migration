import { SfCommand } from '@salesforce/sf-plugins-core';
import { Messages } from '@salesforce/core';
import {
  commonFlags,
  executeMigration,
  sourceFlags,
  targetFlags,
  type MigrationOutput,
} from '../../utils/migration.js';

Messages.importMessagesDirectoryFromMetaUrl(import.meta.url);
const messages = Messages.loadMessages('@haribaskarvr/rca-cml-migration', 'cml.migrate');

export default class CmlMigrate extends SfCommand<MigrationOutput> {
  public static readonly summary = messages.getMessage('summary');
  public static readonly description = messages.getMessage('description');
  public static readonly examples = messages.getMessages('examples');

  public static readonly flags = {
    ...commonFlags(),
    ...sourceFlags(),
    ...targetFlags(),
  };

  public async run(): Promise<MigrationOutput> {
    const { flags } = await this.parse(CmlMigrate);
    return executeMigration('migrate', flags, this.jsonEnabled(), (message, exitCode) =>
      this.error(message, { exit: exitCode })
    );
  }
}
