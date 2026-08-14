#!/usr/bin/env node
// Bundle the render-smoke entry with rolldown (the same bundler vite
// uses here), import the result, and run it. Exit non-zero on any render
// crash - see src/renderSmoke.entry.jsx for why this exists.
import { execFileSync } from 'node:child_process'
import { mkdtempSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, dirname } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const root = join(dirname(fileURLToPath(import.meta.url)), '..')
const out = join(mkdtempSync(join(tmpdir(), 'cb-smoke-')), 'smoke.mjs')
execFileSync(join(root, 'node_modules', '.bin', 'rolldown'),
             ['src/renderSmoke.entry.jsx', '--format', 'esm', '--platform', 'node',
              '--file', out],
             { cwd: root, stdio: ['ignore', 'ignore', 'inherit'] })
const { renderSmoke } = await import(pathToFileURL(out).href)
const size = renderSmoke()
console.log(`render smoke ok - the real message list rendered (${size} chars)`)
