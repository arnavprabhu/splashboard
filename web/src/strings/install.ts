/** Engine install progress and its confirmation (`starting.installing`Splash can't
 * resume a file). Keys start with "install.". Loaded with the lazy install chunk. */
import { makeT } from './index';

export const installStrings = {
  'install.files.one': '{n} file',
  'install.files.other': '{n} files',
  'install.label': 'Model files downloaded',
  'install.of': '{done} of {total}',
  'install.left': '{eta} left',
  'install.warn': 'Stopping now restarts the file in progress from zero; Splash can’t resume it.',
  'install.confirm.title': 'Interrupt the download.',
  'install.confirm.body': 'Splash is downloading {repo} before it loads. Going ahead stops it: finished files are kept, but the file in progress starts again from zero.',
  'install.confirm.go': 'Interrupt',
} as const;

export const t = makeT(installStrings);
