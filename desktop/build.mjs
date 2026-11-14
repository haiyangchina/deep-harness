import { build } from 'esbuild'
import { mkdir, copyFile, readFile } from 'node:fs/promises'

// Single source of truth for the version shown in the UI. The old label was
// hard-coded in main.tsx and stayed at "v0.1" across several releases.
const { version } = JSON.parse(await readFile('package.json', 'utf8'))
await mkdir('dist', { recursive: true })
await build({
  entryPoints: ['electron/main.ts'],
  outfile: 'dist/main.cjs',
  bundle: true,
  platform: 'node',
  external: ['electron']
})
await build({
  entryPoints: ['electron/preload.ts'],
  outfile: 'dist/preload.cjs',
  bundle: true,
  platform: 'node',
  external: ['electron']
})
await build({
  entryPoints: ['src/main.tsx'],
  outfile: 'dist/app.js',
  bundle: true,
  platform: 'browser',
  loader: { '.woff2': 'file' },
  define: { __APP_VERSION__: JSON.stringify(version) }
})
await copyFile('src/index.html', 'dist/index.html')
