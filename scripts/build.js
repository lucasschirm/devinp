#!/usr/bin/env node

/**
 * Bundle the CLI into dependency-free files under bin/:
 *   bin/run.js      — entry point (bundles @oclif/core's loader)
 *   bin/command.js  — the command itself (bundles oclif, ansis, fastify)
 *
 * oclif loads commands.single.target (./bin/command.js) via a runtime
 * import(), so the two bundles each carry their own copy of the deps —
 * that's fine, they communicate through structural APIs only.
 */

import {chmod} from 'node:fs/promises'
import {fileURLToPath} from 'node:url'

import {build} from 'esbuild'

const REQUIRE_SHIM = `import {createRequire} from 'node:module';
const require = createRequire(import.meta.url);`

const shared = {
  bundle: true,
  format: 'esm',
  platform: 'node',
  target: 'node18',
  logLevel: 'warning',
}

await build({
  ...shared,
  entryPoints: [fileURLToPath(new URL('../src/cli.js', import.meta.url))],
  outfile: fileURLToPath(new URL('../bin/run.js', import.meta.url)),
  banner: {js: `#!/usr/bin/env node\n${REQUIRE_SHIM}`},
})

await build({
  ...shared,
  entryPoints: [fileURLToPath(new URL('../src/command.js', import.meta.url))],
  outfile: fileURLToPath(new URL('../bin/command.js', import.meta.url)),
  banner: {js: REQUIRE_SHIM},
})

await chmod(fileURLToPath(new URL('../bin/run.js', import.meta.url)), 0o755)
console.log('built bin/run.js + bin/command.js')
