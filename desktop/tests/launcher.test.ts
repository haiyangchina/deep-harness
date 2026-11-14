import { test } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtemp, mkdir, readFile, rm, writeFile } from 'node:fs/promises'
import { spawnSync } from 'node:child_process'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const launcher = fileURLToPath(new URL('../../scripts/start-desktop.ps1', import.meta.url))
const literal = (value: string) => `'${value.replaceAll("'", "''")}'`

for (const skipInstall of [false, true]) {
  test(
    `Windows PowerShell 5.1 executes the launcher and restores its environment (skip=${skipInstall})`,
    {
      skip: process.platform !== 'win32'
    },
    async () => {
      const directory = await mkdtemp(path.join(tmpdir(), 'next-launcher-'))
      const prefix = path.join(directory, 'isolated env')
      const trace = path.join(directory, 'calls.txt')
      const wrapper = path.join(directory, 'invoke.ps1')
      try {
        await mkdir(prefix)
        await writeFile(path.join(prefix, 'python.exe'), '')
        await writeFile(
          wrapper,
          `
$ErrorActionPreference = 'Stop'
$env:CONDA_PREFIX = 'original-base-environment'
$originalLocation = (Get-Location).Path
function conda {
  $global:LASTEXITCODE = 0
  Write-Output ${literal(path.join(prefix, 'python.exe'))}
}
function npm.cmd {
  param([string]$Verb)
  if ($env:CONDA_PREFIX -ne ${literal(prefix)}) { throw 'Wrong Python environment' }
  if ((Get-Location).Path -ne ${literal(path.resolve(path.dirname(launcher), '../desktop'))}) { throw 'Wrong working directory' }
  Add-Content -LiteralPath ${literal(trace)} -Value $Verb
  $global:LASTEXITCODE = 0
}
try {
  . ${literal(launcher)} ${skipInstall ? '-SkipInstall' : ''}
  if ($env:CONDA_PREFIX -ne 'original-base-environment') { throw 'Environment was not restored' }
  if ((Get-Location).Path -ne $originalLocation) { throw 'Working directory was not restored' }
} catch { Write-Error $_; exit 1 }
`,
          'ascii'
        )
        const result = spawnSync(
          'powershell.exe',
          ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', wrapper],
          {
            encoding: 'utf8',
            timeout: 30_000,
            windowsHide: true
          }
        )
        assert.equal(result.status, 0, result.stderr || result.stdout)
        assert.deepEqual(
          (await readFile(trace, 'utf8')).trim().split(/\r?\n/),
          skipInstall ? ['start'] : ['ci', 'start']
        )
      } finally {
        await rm(directory, { recursive: true, force: true })
      }
    }
  )
}
