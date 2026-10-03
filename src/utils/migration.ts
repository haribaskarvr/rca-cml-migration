import { spawn, spawnSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import type { Interfaces } from '@oclif/core';
import { Flags } from '@salesforce/sf-plugins-core';

type PythonCommand = {
  command: string;
  prefix: string[];
};

const pythonCommands: PythonCommand[] = process.platform === 'win32'
  ? [
      { command: 'python', prefix: [] },
      { command: 'py', prefix: ['-3'] },
    ]
  : [
      { command: 'python3', prefix: [] },
      { command: 'python', prefix: [] },
    ];

const minimumPythonVersion = [3, 10];
type FlagMap = Interfaces.FlagInput;

export function commonFlags(): FlagMap {
  return {
    'auth-mode': Flags.string({
      options: ['sf-cli', 'jwt'],
      default: 'sf-cli',
      summary: 'Reuse authenticated sf aliases or authenticate using role-specific JWT environment variables.',
    }),
    'api-version': Flags.string({ summary: 'Salesforce REST API version; defaults to the latest available per org.' }),
  };
}

export function sourceFlags(): FlagMap {
  return {
    'source-org': Flags.string({ required: true, summary: 'Source org alias.' }),
    'model-api': Flags.string({ required: true, summary: 'Parent ExpressionSet API name.' }),
    'source-version': Flags.string({ summary: 'ExpressionSetDefinitionVersion ID or exposed version number.' }),
    'product-key': Flags.string({ default: 'External_Id__c', summary: 'Stable Product2 matching field.' }),
    'output-dir': Flags.string({ required: true, summary: 'Parent directory for this run’s timestamped artifacts.' }),
  };
}

export function targetFlags(): FlagMap {
  return {
    'target-org': Flags.string({ required: true, summary: 'Target org alias.' }),
    'target-model-api': Flags.string({ summary: 'Override the target model API name.' }),
    'context-definition': Flags.string({ summary: 'Override the context definition developer name.' }),
    'mapping-file': Flags.string({ summary: 'JSON file mapping exported catalog IDs to target IDs.' }),
    'update-existing': Flags.boolean({ summary: 'Allow updating an explicitly selected inactive target version.' }),
    'target-version': Flags.string({ summary: 'Definition-version ID or number used with --update-existing.' }),
    'dry-run': Flags.boolean({ summary: 'Validate and report without target-org writes (the default).' }),
    apply: Flags.boolean({ summary: 'Apply the preflighted plan to the target org.' }),
    'allow-production': Flags.boolean({ summary: 'Additional opt-in required before production writes.' }),
  };
}

export function importFlags(): FlagMap {
  return {
    ...targetFlags(),
    'input-dir': Flags.string({ required: true, summary: 'Exported run directory containing model.cml and manifest.json.' }),
    'output-dir': Flags.string({ summary: 'Parent directory for this import invocation’s run folder.' }),
  };
}

function findPython(): PythonCommand | undefined {
  for (const candidate of pythonCommands) {
    const result = spawnSync(candidate.command, [...candidate.prefix, '--version'], {
      encoding: 'utf8',
      windowsHide: true,
    });
    const versionText = `${result.stdout ?? ''} ${result.stderr ?? ''}`;
    const match = versionText.match(/Python (\d+)\.(\d+)/);
    if (!match || result.status !== 0) continue;

    const version = [Number(match[1]), Number(match[2])];
    if (version[0] > minimumPythonVersion[0]
      || (version[0] === minimumPythonVersion[0] && version[1] >= minimumPythonVersion[1])) {
      return candidate;
    }
  }
}

export type MigrationOutput = {
  exitCode: number;
  stdout: string;
  stderr: string;
};

export function migrationArgs(flags: object): string[] {
  const args: string[] = [];
  for (const [name, value] of Object.entries(flags)) {
    if (name === 'json' || value === undefined || value === false) continue;
    args.push(`--${name}`);
    if (value !== true) args.push(String(value));
  }
  return args;
}

export function runMigrationScript(command: string, args: string[], jsonEnabled: boolean): Promise<MigrationOutput> {
  const scriptPath = resolve(dirname(fileURLToPath(import.meta.url)), '../../scripts/python/migrate_cml.py');
  if (!existsSync(scriptPath)) {
    throw new Error(`Bundled migration script was not found: ${scriptPath}`);
  }

  const python = findPython();
  if (!python) {
    throw new Error('Python 3.10 or newer is required. Install Python and ensure python3, python, or (on Windows) py is on PATH.');
  }

  return new Promise((resolvePromise, rejectPromise) => {
    const child = spawn(python.command, [...python.prefix, scriptPath, command, ...args], {
      cwd: process.cwd(),
      env: process.env,
      stdio: ['inherit', 'pipe', 'pipe'],
      windowsHide: false,
    });
    let stdout = '';
    let stderr = '';

    child.stdout.on('data', (chunk: Buffer) => {
      if (jsonEnabled) stdout += chunk.toString('utf8');
      else process.stdout.write(chunk);
    });
    child.stderr.on('data', (chunk: Buffer) => {
      if (jsonEnabled) stderr += chunk.toString('utf8');
      else process.stderr.write(chunk);
    });
    child.on('error', (error) => rejectPromise(new Error(`Unable to start the Python migration engine: ${error.message}`)));
    child.on('close', (exitCode) => resolvePromise({ exitCode: exitCode ?? 1, stdout, stderr }));
  });
}

export async function executeMigration(
  command: string,
  flags: object,
  jsonEnabled: boolean,
  reportError: (message: string, exitCode: number) => void,
): Promise<MigrationOutput> {
  const result = await runMigrationScript(command, migrationArgs(flags), jsonEnabled);
  if (result.exitCode !== 0) {
    const message = jsonEnabled && result.stderr.trim()
      ? result.stderr.trim()
      : `CML ${command} failed with exit code ${result.exitCode}.`;
    reportError(message, result.exitCode);
  }

  return result;
}
