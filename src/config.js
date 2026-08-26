import 'dotenv/config'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const projectRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')

function integerFromEnv(name, fallback, minimum) {
  const value = Number.parseInt(process.env[name] ?? '', 10)
  return Number.isFinite(value) && value >= minimum ? value : fallback
}

export const config = Object.freeze({
  projectRoot,
  host: process.env.HOST?.trim() || '127.0.0.1',
  port: integerFromEnv('PORT', 3001, 1),
  apiKey: process.env.API_KEY?.trim() || '',
  authDirectory: path.join(projectRoot, 'data', 'baileys-auth'),
  sendIntervalMs: integerFromEnv('SEND_INTERVAL_MS', 3000, 1000),
  duplicateWindowMs: integerFromEnv('DUPLICATE_WINDOW_MS', 300000, 0),
  defaultMessage:
    process.env.DEFAULT_MESSAGE?.trim() ||
    'Ola {{nome}}, tudo bem? Gostaria de falar com a equipe da {{empresa}}. Podemos conversar?'
})

