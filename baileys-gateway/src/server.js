import { createHash, timingSafeEqual } from 'node:crypto'
import { mkdir, readFile, readdir, writeFile } from 'node:fs/promises'
import { createServer } from 'node:http'
import path from 'node:path'

import makeWASocket, { Browsers, DisconnectReason, useMultiFileAuthState } from 'baileys'
import pino from 'pino'

const port = Number(process.env.PORT || 8080)
const apiToken = process.env.BAILEYS_GATEWAY_TOKEN || ''
const authRoot = path.resolve(process.env.BAILEYS_AUTH_DIR || './data/sessions')
const logger = pino({ level: process.env.LOG_LEVEL || 'info' })
const sessions = new Map()

if (apiToken.length < 32) {
  throw new Error('BAILEYS_GATEWAY_TOKEN precisa ter pelo menos 32 caracteres.')
}

await mkdir(authRoot, { recursive: true })

function safeSessionId(value) {
  const sessionId = String(value || '').trim()
  if (!/^[a-zA-Z0-9_-]{4,80}$/.test(sessionId)) throw new HttpError(400, 'sessionId inválido.')
  return sessionId
}

function sessionDir(sessionId) {
  return path.join(authRoot, safeSessionId(sessionId))
}

function secureEqual(left, right) {
  const leftHash = createHash('sha256').update(left).digest()
  const rightHash = createHash('sha256').update(right).digest()
  return timingSafeEqual(leftHash, rightHash)
}

function authorized(request) {
  const value = request.headers.authorization || ''
  return value.startsWith('Bearer ') && secureEqual(value.slice(7), apiToken)
}

class HttpError extends Error {
  constructor(status, message) {
    super(message)
    this.status = status
  }
}

async function readJson(file, fallback) {
  try {
    return JSON.parse(await readFile(file, 'utf8'))
  } catch (error) {
    if (error.code === 'ENOENT') return fallback
    throw error
  }
}

async function saveJson(file, value) {
  await writeFile(file, JSON.stringify(value, null, 2), { mode: 0o600 })
}

async function requestBody(request) {
  let raw = ''
  for await (const chunk of request) {
    raw += chunk
    if (raw.length > 1_000_000) throw new HttpError(413, 'Corpo da requisição muito grande.')
  }
  if (!raw) return {}
  try {
    return JSON.parse(raw)
  } catch {
    throw new HttpError(400, 'JSON inválido.')
  }
}

function sendJson(response, status, payload) {
  const body = JSON.stringify(payload)
  response.writeHead(status, {
    'Content-Type': 'application/json; charset=utf-8',
    'Content-Length': Buffer.byteLength(body),
    'Cache-Control': 'no-store',
  })
  response.end(body)
}

function snapshot(entry) {
  return {
    sessionId: entry.sessionId,
    status: entry.status || 'disconnected',
    connected: entry.status === 'connected',
    phone: entry.phone || null,
    qrcode: entry.qrcode || null,
    lastError: entry.lastError || null,
  }
}

async function emitWebhook(entry, event, data) {
  if (!entry.config?.webhookUrl) return
  try {
    const response = await fetch(entry.config.webhookUrl, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Baileys-Webhook-Secret': entry.config.webhookSecret || '',
      },
      body: JSON.stringify({ event, sessionId: entry.sessionId, data }),
      signal: AbortSignal.timeout(15_000),
    })
    if (!response.ok) logger.warn({ sessionId: entry.sessionId, event, status: response.status }, 'webhook recusado')
  } catch (error) {
    logger.warn({ sessionId: entry.sessionId, event, error: String(error) }, 'falha ao enviar webhook')
  }
}

function messageText(message) {
  return message?.conversation
    || message?.extendedTextMessage?.text
    || message?.imageMessage?.caption
    || message?.videoMessage?.caption
    || ''
}

async function connectSession(sessionId, config = null) {
  sessionId = safeSessionId(sessionId)
  let entry = sessions.get(sessionId)
  if (!entry) {
    const directory = sessionDir(sessionId)
    await mkdir(directory, { recursive: true })
    entry = {
      sessionId,
      directory,
      status: 'disconnected',
      config: await readJson(path.join(directory, 'nexo-config.json'), {}),
      sent: await readJson(path.join(directory, 'sent-requests.json'), {}),
    }
    sessions.set(sessionId, entry)
  }
  if (config) {
    const webhookUrl = String(config.webhookUrl || '').trim()
    if (webhookUrl && !webhookUrl.startsWith('https://')) throw new HttpError(400, 'webhookUrl precisa usar HTTPS.')
    entry.config = { webhookUrl, webhookSecret: String(config.webhookSecret || '') }
    await saveJson(path.join(entry.directory, 'nexo-config.json'), entry.config)
  }
  if (entry.sock || entry.connecting) return entry

  entry.connecting = true
  entry.status = 'connecting'
  entry.lastError = null
  const { state, saveCreds } = await useMultiFileAuthState(entry.directory)
  const sock = makeWASocket({
    auth: state,
    browser: Browsers.ubuntu('Nexo Flow'),
    logger: logger.child({ sessionId }),
    markOnlineOnConnect: false,
    printQRInTerminal: false,
    syncFullHistory: false,
  })
  entry.sock = sock
  entry.connecting = false

  sock.ev.on('creds.update', saveCreds)
  sock.ev.on('connection.update', async ({ connection, lastDisconnect, qr }) => {
    if (qr) {
      entry.qrcode = qr
      entry.status = 'connecting'
      await emitWebhook(entry, 'connection', snapshot(entry))
    }
    if (connection === 'open') {
      entry.status = 'connected'
      entry.qrcode = null
      entry.phone = String(sock.user?.id || '').split(':', 1)[0].split('@', 1)[0] || null
      entry.lastError = null
      logger.info({ sessionId, phone: entry.phone }, 'sessão conectada')
      await emitWebhook(entry, 'connection', snapshot(entry))
    }
    if (connection === 'close') {
      const code = lastDisconnect?.error?.output?.statusCode
      const loggedOut = code === DisconnectReason.loggedOut
      entry.sock = null
      entry.status = 'disconnected'
      entry.qrcode = null
      entry.lastError = loggedOut ? 'Sessão desconectada pelo WhatsApp.' : String(lastDisconnect?.error || 'Conexão encerrada.')
      await emitWebhook(entry, 'connection', snapshot(entry))
      if (!loggedOut) {
        const timer = setTimeout(() => connectSession(sessionId).catch(error => logger.error({ sessionId, error: String(error) }, 'reconexão falhou')), 3_000)
        timer.unref()
      }
    }
  })

  sock.ev.on('messages.upsert', async ({ messages }) => {
    for (const message of messages) {
      const jid = String(message.key?.remoteJid || '')
      if (!message.message || message.key?.fromMe || jid.endsWith('@g.us') || jid === 'status@broadcast') continue
      await emitWebhook(entry, 'message', {
        id: message.key?.id,
        from: jid.split('@', 1)[0],
        text: messageText(message.message),
        type: Object.keys(message.message)[0] || 'message',
        timestamp: Number(message.messageTimestamp || Date.now() / 1000),
      })
    }
  })
  return entry
}

async function waitForConnected(entry, timeoutMs = 20_000) {
  if (entry.status === 'connected' && entry.sock) return
  const started = Date.now()
  while (Date.now() - started < timeoutMs) {
    if (entry.status === 'connected' && entry.sock) return
    await new Promise(resolve => setTimeout(resolve, 250))
  }
  throw new HttpError(409, 'A sessão Baileys não está conectada.')
}

async function route(request, response) {
  const url = new URL(request.url, `http://${request.headers.host || 'localhost'}`)
  if (request.method === 'GET' && url.pathname === '/health') {
    return sendJson(response, 200, { status: 'ok', provider: 'baileys', sessions: sessions.size })
  }
  if (!authorized(request)) throw new HttpError(401, 'Não autorizado.')

  const match = url.pathname.match(/^\/sessions\/([a-zA-Z0-9_-]{4,80})(?:\/(status|connect|check|messages))?$/)
  if (!match) throw new HttpError(404, 'Rota não encontrada.')
  const sessionId = safeSessionId(match[1])
  const action = match[2] || ''

  if (request.method === 'POST' && !action) {
    const body = await requestBody(request)
    const entry = await connectSession(sessionId, body)
    return sendJson(response, 201, snapshot(entry))
  }
  if (request.method === 'GET' && action === 'status') {
    const entry = await connectSession(sessionId)
    return sendJson(response, 200, snapshot(entry))
  }
  if (request.method === 'POST' && action === 'connect') {
    const entry = await connectSession(sessionId)
    return sendJson(response, 200, snapshot(entry))
  }
  if (request.method === 'POST' && action === 'check') {
    const body = await requestBody(request)
    const entry = await connectSession(sessionId)
    await waitForConnected(entry)
    const numbers = Array.isArray(body.numbers) ? body.numbers.map(value => String(value).replace(/\D/g, '')).filter(Boolean) : []
    if (!numbers.length) throw new HttpError(400, 'Informe numbers.')
    const result = await entry.sock.onWhatsApp(...numbers)
    return sendJson(response, 200, { results: result.map(item => ({ query: item.jid?.split('@', 1)[0], jid: item.jid, exists: Boolean(item.exists) })) })
  }
  if (request.method === 'POST' && action === 'messages') {
    const body = await requestBody(request)
    const entry = await connectSession(sessionId)
    await waitForConnected(entry)
    const to = String(body.to || '').replace(/\D/g, '')
    const text = String(body.text || '').trim()
    const requestId = String(body.requestId || '').trim()
    if (!to || !text || !requestId) throw new HttpError(400, 'Informe to, text e requestId.')
    if (entry.sent[requestId]) return sendJson(response, 200, { id: entry.sent[requestId], duplicate: true })
    const [checked] = await entry.sock.onWhatsApp(to)
    if (!checked?.exists) throw new HttpError(422, 'O número do destinatário não está cadastrado no WhatsApp.')
    const sent = await entry.sock.sendMessage(checked.jid, { text })
    const messageId = sent?.key?.id
    if (!messageId) throw new HttpError(502, 'O WhatsApp não retornou o ID da mensagem.')
    entry.sent[requestId] = messageId
    await saveJson(path.join(entry.directory, 'sent-requests.json'), entry.sent)
    return sendJson(response, 200, { id: messageId })
  }
  throw new HttpError(405, 'Método não permitido.')
}

const server = createServer((request, response) => {
  route(request, response).catch(error => {
    const status = error instanceof HttpError ? error.status : 500
    logger[status >= 500 ? 'error' : 'warn']({ status, error: String(error) }, 'requisição falhou')
    sendJson(response, status, { error: error.message || 'Erro interno.' })
  })
})

server.listen(port, '0.0.0.0', async () => {
  logger.info({ port, authRoot }, 'gateway Baileys iniciado')
  for (const entry of await readdir(authRoot, { withFileTypes: true })) {
    if (entry.isDirectory() && /^[a-zA-Z0-9_-]{4,80}$/.test(entry.name)) {
      connectSession(entry.name).catch(error => logger.error({ sessionId: entry.name, error: String(error) }, 'restauração de sessão falhou'))
    }
  }
})

for (const signal of ['SIGINT', 'SIGTERM']) {
  process.on(signal, () => {
    server.close(() => process.exit(0))
  })
}
