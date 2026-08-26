import { EventEmitter } from 'node:events'
import { rm } from 'node:fs/promises'
import { Boom } from '@hapi/boom'
import makeWASocket, {
  Browsers,
  DisconnectReason,
  makeCacheableSignalKeyStore,
  useMultiFileAuthState
} from '@whiskeysockets/baileys'
import pino from 'pino'
import QRCode from 'qrcode'
import { assertMessage, normalizePhone, renderMessage } from './message.js'

const logger = pino({ level: process.env.LOG_LEVEL || 'warn' })

function wait(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds))
}

export class WhatsAppService extends EventEmitter {
  constructor(options) {
    super()
    this.options = options
    this.socket = null
    this.status = 'starting'
    this.qrDataUrl = null
    this.lastError = null
    this.connectedPhone = null
    this.connectionGeneration = 0
    this.reconnectTimer = null
    this.startPromise = null
    this.sendQueue = Promise.resolve()
    this.lastSentAt = 0
    this.recentMessages = new Map()
  }

  snapshot() {
    return {
      status: this.status,
      connected: this.status === 'connected',
      phone: this.connectedPhone,
      hasQrCode: Boolean(this.qrDataUrl),
      qrDataUrl: this.qrDataUrl,
      error: this.lastError
    }
  }

  async start() {
    if (this.startPromise) return this.startPromise

    clearTimeout(this.reconnectTimer)
    this.startPromise = this.#connect().finally(() => {
      this.startPromise = null
    })
    return this.startPromise
  }

  async #connect() {
    const generation = ++this.connectionGeneration
    this.status = 'connecting'
    this.lastError = null
    this.emit('status', this.snapshot())

    const { state, saveCreds } = await useMultiFileAuthState(this.options.authDirectory)
    const socket = makeWASocket({
      auth: {
        creds: state.creds,
        keys: makeCacheableSignalKeyStore(state.keys, logger)
      },
      browser: Browsers.windows('Nexo Flow'),
      logger,
      markOnlineOnConnect: false,
      syncFullHistory: false
    })

    this.socket = socket
    socket.ev.on('creds.update', saveCreds)
    socket.ev.on('connection.update', async (update) => {
      if (generation !== this.connectionGeneration) return

      if (update.qr) {
        this.qrDataUrl = await QRCode.toDataURL(update.qr, {
          errorCorrectionLevel: 'M',
          margin: 2,
          width: 360
        })
        this.status = 'qr_ready'
        this.emit('status', this.snapshot())
      }

      if (update.connection === 'open') {
        this.status = 'connected'
        this.qrDataUrl = null
        this.lastError = null
        this.connectedPhone = socket.user?.id?.split(':')[0]?.split('@')[0] || null
        this.emit('status', this.snapshot())
        return
      }

      if (update.connection === 'close') {
        this.socket = null
        this.qrDataUrl = null
        this.connectedPhone = null

        const statusCode = new Boom(update.lastDisconnect?.error).output.statusCode
        const loggedOut = statusCode === DisconnectReason.loggedOut
        this.status = loggedOut ? 'logged_out' : 'reconnecting'
        this.lastError = loggedOut
          ? 'Sessao desconectada. Gere um novo QR Code.'
          : 'Conexao interrompida; tentando reconectar.'
        this.emit('status', this.snapshot())

        if (!loggedOut) {
          clearTimeout(this.reconnectTimer)
          this.reconnectTimer = setTimeout(() => this.start().catch((error) => {
            this.#setFailure(error)
          }), 2000)
        }
      }
    })
  }

  #setFailure(error) {
    this.status = 'error'
    this.lastError = error instanceof Error ? error.message : String(error)
    this.emit('status', this.snapshot())
  }

  async reconnect() {
    if (this.status === 'connected') return this.snapshot()
    await this.start()
    return this.snapshot()
  }

  async logout() {
    clearTimeout(this.reconnectTimer)
    this.connectionGeneration += 1
    const currentSocket = this.socket
    this.socket = null

    if (currentSocket) {
      try {
        await currentSocket.logout()
      } catch (error) {
        logger.debug({ error }, 'Nao foi possivel encerrar a sessao remotamente')
      }
    }

    await rm(this.options.authDirectory, { recursive: true, force: true })
    this.status = 'logged_out'
    this.qrDataUrl = null
    this.connectedPhone = null
    this.lastError = null
    this.emit('status', this.snapshot())
    return this.snapshot()
  }

  async sendStandardMessage(input) {
    const task = async () => {
      if (this.status !== 'connected' || !this.socket) {
        const error = new Error('O WhatsApp ainda nao esta conectado.')
        error.statusCode = 409
        throw error
      }

      if (input.consent !== true) {
        const error = new Error('Confirme que o contato autorizou o recebimento da mensagem.')
        error.statusCode = 422
        throw error
      }

      const phone = normalizePhone(input.phone)
      const message = renderMessage(input.message || this.options.defaultMessage, input)
      assertMessage(message)

      const duplicateKey = `${phone}\n${message}`
      const now = Date.now()
      const previousSend = this.recentMessages.get(duplicateKey)
      if (previousSend && now - previousSend < this.options.duplicateWindowMs) {
        const error = new Error('Esta mesma mensagem ja foi enviada recentemente para esse numero.')
        error.statusCode = 409
        throw error
      }

      const elapsed = now - this.lastSentAt
      if (elapsed < this.options.sendIntervalMs) {
        await wait(this.options.sendIntervalMs - elapsed)
      }

      const matches = await this.socket.onWhatsApp(phone)
      const recipient = matches?.find((entry) => entry.exists)
      if (!recipient?.jid) {
        const error = new Error('Esse numero nao foi encontrado no WhatsApp.')
        error.statusCode = 404
        throw error
      }

      const result = await this.socket.sendMessage(recipient.jid, { text: message })
      this.lastSentAt = Date.now()
      this.recentMessages.set(duplicateKey, this.lastSentAt)
      this.#pruneRecentMessages()

      return {
        id: result?.key?.id || null,
        phone,
        jid: recipient.jid,
        message,
        sentAt: new Date(this.lastSentAt).toISOString()
      }
    }

    const result = this.sendQueue.then(task, task)
    this.sendQueue = result.catch(() => undefined)
    return result
  }

  #pruneRecentMessages() {
    const cutoff = Date.now() - this.options.duplicateWindowMs
    for (const [key, timestamp] of this.recentMessages) {
      if (timestamp < cutoff) this.recentMessages.delete(key)
    }
  }
}

