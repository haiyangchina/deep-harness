import { build } from 'esbuild'
import { mkdir, copyFile, readFile } from 'node:fs/promises'

// Both deployments ship the same workbench bundle.
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

const webOutput = '../src/deep_harness/transports/web_static'
await mkdir(webOutput, { recursive: true })
await Promise.all(['index.html', 'app.js', 'app.css'].map(
  name => copyFile(`dist/${name}`, `${webOutput}/${name}`)
))
