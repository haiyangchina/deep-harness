import { build } from 'esbuild'
import { mkdir, copyFile } from 'node:fs/promises'
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
  loader: { '.woff2': 'file' }
})
await copyFile('src/index.html', 'dist/index.html')
