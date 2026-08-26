import { createCipheriv, createDecipheriv, createHash, createHmac, randomBytes, timingSafeEqual } from 'node:crypto'

import { waitUntil } from '@vercel/functions'
import makeWASocket, { Browsers, BufferJSON, DisconnectReason, fetchLatestBaileysVersion, initAuthCreds, proto } from 'baileys'
import pg from 'pg'
import pino from 'pino'

export const maxDuration = 300

const databaseUrl = process.env.DATABASE_URL || process.env.POSTGRES_URL
const appSecret = process.env.APP_SECRET || ''
const logger = pino({ level: 'silent' })
const pool = new pg.Pool({ connectionString: databaseUrl, max: 3, ssl: databaseUrl?.includes('localhost') ? false : { rejectUnauthorized: false } })
const encryptionKey = createHash('sha256').update(appSecret).digest()
let schemaPromise

if (!databaseUrl || appSecret.length < 32) throw new Error('DATABASE_URL e APP_SECRET são obrigatórios para o Baileys interno.')

function stableJson(value) {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(',')}]`
  if (value && typeof value === 'object') {
    return `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${stableJson(value[key])}`).join(',')}}`
  }
  return JSON.stringify(value)
}

function secureEqual(left, right) {
  const leftHash = createHash('sha256').update(left).digest()
  const rightHash = createHash('sha256').update(right).digest()
  return timingSafeEqual(leftHash, rightHash)
}

function authorized(request, action, sessionId, body) {
  const timestamp = String(request.headers['x-nexo-timestamp'] || '')
  const signature = String(request.headers['x-nexo-signature'] || '')
  const age = Math.abs(Date.now() - Number(timestamp) * 1000)
  if (!timestamp || !signature || !Number.isFinite(age) || age > 300_000) return false
  const expected = createHmac('sha256', appSecret).update(`${timestamp}.${action}.${sessionId}.${stableJson(body)}`).digest('hex')
  return secureEqual(signature, expected)
}

function encrypt(value) {
  const iv = randomBytes(12)
  const cipher = createCipheriv('aes-256-gcm', encryptionKey, iv)
  const encrypted = Buffer.concat([cipher.update(value, 'utf8'), cipher.final()])
  return `${iv.toString('base64')}.${cipher.getAuthTag().toString('base64')}.${encrypted.toString('base64')}`
}

function decrypt(value) {
  const [iv, tag, encrypted] = String(value).split('.')
  const decipher = createDecipheriv('aes-256-gcm', encryptionKey, Buffer.from(iv, 'base64'))
  decipher.setAuthTag(Buffer.from(tag, 'base64'))
  return Buffer.concat([decipher.update(Buffer.from(encrypted, 'base64')), decipher.final()]).toString('utf8')
}

function encode(value) {
  return encrypt(JSON.stringify(value, BufferJSON.replacer))
}

function decode(value) {
  return JSON.parse(decrypt(value), BufferJSON.reviver)
}

async function ensureSchema() {
  if (!schemaPromise) {
    schemaPromise = pool.query(`
      CREATE TABLE IF NOT EXISTS baileys_auth_state (
        session_id varchar(80) NOT NULL,
        category varchar(80) NOT NULL,
        key_id varchar(220) NOT NULL,
        payload text NOT NULL,
        updated_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (session_id, category, key_id)
      );
      CREATE TABLE IF NOT EXISTS baileys_serverless_sessions (
        session_id varchar(80) PRIMARY KEY,
        status varchar(32) NOT NULL DEFAULT 'disconnected',
        phone varchar(32),
        qr_payload text,
        last_error text,
        generation_id varchar(64),
        updated_at timestamptz NOT NULL DEFAULT now()
      );
      ALTER TABLE baileys_serverless_sessions ADD COLUMN IF NOT EXISTS generation_id varchar(64);
      CREATE TABLE IF NOT EXISTS baileys_sent_requests (
        session_id varchar(80) NOT NULL,
        request_id varchar(180) NOT NULL,
        message_id varchar(220) NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (session_id, request_id)
      );
    `)
  }
  return schemaPromise
}

async function upsertSession(sessionId, values) {
  await ensureSchema()
  const current = await pool.query('SELECT * FROM baileys_serverless_sessions WHERE session_id = $1', [sessionId])
  const row = current.rows[0] || {}
  const status = values.status ?? row.status ?? 'disconnected'
  const phone = values.phone === undefined ? row.phone ?? null : values.phone
  const qr = values.qrcode === undefined ? row.qr_payload ?? null : values.qrcode ? encrypt(values.qrcode) : null
  const error = values.lastError === undefined ? row.last_error ?? null : values.lastError
  const generationId = values.generationId === undefined ? row.generation_id ?? null : values.generationId
  await pool.query(
    `INSERT INTO baileys_serverless_sessions (session_id, status, phone, qr_payload, last_error, generation_id, updated_at)
     VALUES ($1,$2,$3,$4,$5,$6,now())
     ON CONFLICT (session_id) DO UPDATE SET status=EXCLUDED.status, phone=EXCLUDED.phone,
       qr_payload=EXCLUDED.qr_payload, last_error=EXCLUDED.last_error, generation_id=EXCLUDED.generation_id, updated_at=now()`,
    [sessionId, status, phone, qr, error, generationId],
  )
  return true
}

async function updateAttemptSession(sessionId, generationId, values) {
  await ensureSchema()
  const current = await pool.query(
    'SELECT * FROM baileys_serverless_sessions WHERE session_id=$1 AND generation_id=$2',
    [sessionId, generationId],
  )
  const row = current.rows[0]
  if (!row) return false
  const status = values.status ?? row.status
  const phone = values.phone === undefined ? row.phone : values.phone
  const qr = values.qrcode === undefined ? row.qr_payload : values.qrcode ? encrypt(values.qrcode) : null
  const error = values.lastError === undefined ? row.last_error : values.lastError
  const result = await pool.query(
    `UPDATE baileys_serverless_sessions
     SET status=$3, phone=$4, qr_payload=$5, last_error=$6, updated_at=now()
     WHERE session_id=$1 AND generation_id=$2`,
    [sessionId, generationId, status, phone, qr, error],
  )
  return result.rowCount === 1
}

async function sessionSnapshot(sessionId) {
  await ensureSchema()
  const result = await pool.query('SELECT * FROM baileys_serverless_sessions WHERE session_id = $1', [sessionId])
  const row = result.rows[0]
  if (!row) return { sessionId, status: 'disconnected', connected: false, phone: null, qrcode: null, lastError: null }
  let qrcode = null
  try { qrcode = row.qr_payload ? decrypt(row.qr_payload) : null } catch { qrcode = null }
  return { sessionId, status: row.status, connected: row.status === 'connected', phone: row.phone, qrcode, lastError: row.last_error }
}

async function authState(sessionId) {
  await ensureSchema()
  const credsResult = await pool.query(
    `SELECT payload FROM baileys_auth_state WHERE session_id=$1 AND category='creds' AND key_id='creds'`,
    [sessionId],
  )
  const creds = credsResult.rows[0] ? decode(credsResult.rows[0].payload) : initAuthCreds()
  return {
    state: {
      creds,
      keys: {
        get: async (type, ids) => {
          if (!ids.length) return {}
          const result = await pool.query(
            'SELECT key_id, payload FROM baileys_auth_state WHERE session_id=$1 AND category=$2 AND key_id = ANY($3)',
            [sessionId, type, ids],
          )
          const values = {}
          for (const row of result.rows) {
            let value = decode(row.payload)
            if (type === 'app-state-sync-key' && value) value = proto.Message.AppStateSyncKeyData.fromObject(value)
            values[row.key_id] = value
          }
          return values
        },
        set: async data => {
          const client = await pool.connect()
          try {
            await client.query('BEGIN')
            for (const [category, entries] of Object.entries(data)) {
              for (const [keyId, value] of Object.entries(entries)) {
                if (value == null) {
                  await client.query('DELETE FROM baileys_auth_state WHERE session_id=$1 AND category=$2 AND key_id=$3', [sessionId, category, keyId])
                } else {
                  await client.query(
                    `INSERT INTO baileys_auth_state (session_id, category, key_id, payload, updated_at) VALUES ($1,$2,$3,$4,now())
                     ON CONFLICT (session_id, category, key_id) DO UPDATE SET payload=EXCLUDED.payload, updated_at=now()`,
                    [sessionId, category, keyId, encode(value)],
                  )
                }
              }
            }
            await client.query('COMMIT')
          } catch (error) {
            await client.query('ROLLBACK')
            throw error
          } finally {
            client.release()
          }
        },
      },
    },
    saveCreds: async () => {
      await pool.query(
        `INSERT INTO baileys_auth_state (session_id, category, key_id, payload, updated_at) VALUES ($1,'creds','creds',$2,now())
         ON CONFLICT (session_id, category, key_id) DO UPDATE SET payload=EXCLUDED.payload, updated_at=now()`,
        [sessionId, encode(creds)],
      )
    },
  }
}

async function clearAuth(sessionId) {
  await ensureSchema()
  await pool.query('DELETE FROM baileys_auth_state WHERE session_id=$1', [sessionId])
}

async function openSocket(sessionId, timeoutMs = 240_000, generationId = null) {
  const { state, saveCreds } = await authState(sessionId)

  const updateSession = values => generationId
    ? updateAttemptSession(sessionId, generationId, values)
    : upsertSession(sessionId, values)

  let manualClose = false
  let reconnecting = false
  let currentSock = null

  let settleFirst
  let settleLifetime

  const first = new Promise(resolve => {
    settleFirst = resolve
  })

  const lifetime = new Promise(resolve => {
    settleLifetime = resolve
  })

  const startSocket = async () => {
    if (manualClose) return

    const { version, isLatest } = await fetchLatestBaileysVersion()

    console.info('[Baileys] starting socket', {
      sessionId,
      version: version.join('.'),
      isLatest,
    })

    const sock = makeWASocket({
      version,
      auth: state,
      browser: Browsers.ubuntu('Nexo Flow'),
      logger,
      markOnlineOnConnect: false,
      printQRInTerminal: false,
      syncFullHistory: false,
    })

    currentSock = sock

    sock.ev.on('creds.update', saveCreds)

    sock.ev.on('connection.update', async update => {
      const {
        connection,
        lastDisconnect,
        qr,
      } = update

      if (qr) {
        const updated = await updateSession({
          status: 'connecting',
          qrcode: qr,
          lastError: null,
        })

        if (!updated) return

        console.info('[Baileys] QR generated', {
          sessionId,
          qrLength: qr.length,
        })

        settleFirst({
          kind: 'qr',
          qrcode: qr,
        })
      }

      if (connection === 'open') {
        const phone =
          String(sock.user?.id || '')
            .split(':', 1)[0]
            .split('@', 1)[0] || null

        const updated = await updateSession({
          status: 'connected',
          phone,
          qrcode: null,
          lastError: null,
        })

        if (!updated) return

        console.info('[Baileys] connection opened', {
          sessionId,
          phone,
        })

        settleFirst({
          kind: 'open',
          phone,
        })

        settleLifetime()
        return
      }

      if (connection === 'close' && !manualClose) {
        const code =
          lastDisconnect?.error?.output?.statusCode ||
          lastDisconnect?.error?.data?.statusCode ||
          null

        console.warn('[Baileys] connection closed', {
          sessionId,
          code,
          error: String(lastDisconnect?.error || ''),
        })

        if (code === DisconnectReason.loggedOut) {
          await clearAuth(sessionId)

          await updateSession({
            status: 'disconnected',
            qrcode: null,
            lastError: 'Sessão removida pelo WhatsApp.',
          })

          settleFirst({
            kind: 'close',
            error: 'Sessão removida pelo WhatsApp.',
          })

          settleLifetime()
          return
        }

        await updateSession({
          status: 'connecting',
          qrcode: null,
          lastError: null,
        })

        if (!reconnecting) {
          reconnecting = true

          setTimeout(async () => {
            reconnecting = false

            if (manualClose) return

            try {
              console.info('[Baileys] reconnecting', {
                sessionId,
                code,
              })

              await startSocket()
            } catch (error) {
              console.error('[Baileys] reconnect failed', {
                sessionId,
                error: String(error),
              })

              await updateSession({
                status: 'disconnected',
                qrcode: null,
                lastError: String(error),
              })

              settleFirst({
                kind: 'close',
                error: String(error),
              })

              settleLifetime()
            }
          }, 1200)
        }
      }
    })
  }

  await startSocket()

  const timer = setTimeout(async () => {
    if (!manualClose) {
      manualClose = true

      await updateSession({
        status: 'disconnected',
        qrcode: null,
        lastError: 'O QR expirou. Gere uma nova conexão.',
      })

      console.info('[Baileys] QR expired', {
        sessionId,
      })
    }

    settleFirst({
      kind: 'timeout',
    })

    settleLifetime()

    try {
      currentSock?.end(new Error('timeout'))
    } catch {}
  }, timeoutMs)

  return {
    get sock() {
      return currentSock
    },

    first,

    lifetime: lifetime.finally(() => {
      clearTimeout(timer)
    }),

    close: () => {
      manualClose = true
      clearTimeout(timer)

      try {
        currentSock?.end(new Error('request complete'))
      } catch {}

      settleLifetime()
    },
  }
}

async function waitForOpen(socketHandle, timeoutMs = 25_000) {
  const result = await Promise.race([
    socketHandle.first,
    new Promise(resolve => setTimeout(() => resolve({ kind: 'timeout' }), timeoutMs)),
  ])
  if (result.kind !== 'open') throw new Error(result.kind === 'qr' ? 'A sessão precisa ser pareada pelo QR.' : result.error || 'Não foi possível conectar a sessão.')
}

async function handleAction(action, sessionId, body) {
  if (action === 'status') return sessionSnapshot(sessionId)
  if (action === 'connect' || action === 'create') {
    const generationId = randomBytes(16).toString('hex')
    await upsertSession(sessionId, { status: 'connecting', qrcode: null, lastError: null, generationId })
    const handle = await openSocket(sessionId, 240_000, generationId)
    const first = await Promise.race([handle.first, new Promise(resolve => setTimeout(() => resolve({ kind: 'timeout' }), 25_000))])
    waitUntil(handle.lifetime)
    if (first.kind === 'timeout') throw new Error('O WhatsApp não gerou o QR a tempo.')
    return sessionSnapshot(sessionId)
  }
  if (action === 'check') {
    const handle = await openSocket(sessionId, 40_000)
    try {
      await waitForOpen(handle)
      const numbers = Array.isArray(body.numbers) ? body.numbers.map(value => String(value).replace(/\D/g, '')).filter(Boolean) : []
      const result = await handle.sock.onWhatsApp(...numbers)
      return { results: result.map(item => ({ query: item.jid?.split('@', 1)[0], jid: item.jid, exists: Boolean(item.exists) })) }
    } finally { handle.close() }
  }
  if (action === 'messages') {
    await ensureSchema()
    const existing = await pool.query('SELECT message_id FROM baileys_sent_requests WHERE session_id=$1 AND request_id=$2', [sessionId, body.requestId])
    if (existing.rows[0]) return { id: existing.rows[0].message_id, duplicate: true }
    const handle = await openSocket(sessionId, 50_000)
    try {
      await waitForOpen(handle)
      const to = String(body.to || '').replace(/\D/g, '')
      const text = String(body.text || '').trim()
      const [checked] = await handle.sock.onWhatsApp(to)
      if (!checked?.exists) {
        const error = new Error('O número do destinatário não está cadastrado no WhatsApp.')
        error.status = 422
        throw error
      }
      const sent = await handle.sock.sendMessage(checked.jid, { text })
      const messageId = sent?.key?.id
      if (!messageId) throw new Error('O WhatsApp não retornou o ID da mensagem.')
      await pool.query(
        'INSERT INTO baileys_sent_requests (session_id, request_id, message_id) VALUES ($1,$2,$3) ON CONFLICT (session_id, request_id) DO NOTHING',
        [sessionId, body.requestId, messageId],
      )
      return { id: messageId }
    } finally { handle.close() }
  }
  const error = new Error('Ação inválida.')
  error.status = 404
  throw error
}

export default async function handler(request, response) {
  if (request.method !== 'POST') return response.status(405).json({ error: 'Método não permitido.' })
  const action = String(request.query.action || '')
  const sessionId = String(request.query.sessionId || '')
  const body = request.body && typeof request.body === 'object' ? request.body : {}
  if (!/^[a-zA-Z0-9_-]{4,80}$/.test(sessionId)) return response.status(400).json({ error: 'sessionId inválido.' })
  if (!authorized(request, action, sessionId, body)) return response.status(401).json({ error: 'Não autorizado.' })
  try {
    const result = await handleAction(action, sessionId, body)
    return response.status(action === 'create' ? 201 : 200).json(result)
  } catch (error) {
    console.error('[baileys]', { action, sessionId, error: String(error) })
    return response.status(error.status || 502).json({ error: error.message || 'Falha no Baileys.' })
  }
}
