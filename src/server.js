import express from 'express'
import { config } from './config.js'
import { WhatsAppService } from './whatsapp-service.js'

const app = express()
const whatsapp = new WhatsAppService(config)

app.disable('x-powered-by')
app.use(express.json({ limit: '32kb' }))
app.use(express.static(`${config.projectRoot}/public`))

function requireApiKey(request, response, next) {
  const isLocalInterface = request.get('sec-fetch-site') === 'same-origin'
  if (!config.apiKey || isLocalInterface || request.get('x-api-key') === config.apiKey) return next()
  return response.status(401).json({ error: 'Chave de API invalida.' })
}

app.get('/api/status', requireApiKey, (_request, response) => {
  response.json(whatsapp.snapshot())
})

app.post('/api/connect', requireApiKey, async (_request, response, next) => {
  try {
    response.status(202).json(await whatsapp.reconnect())
  } catch (error) {
    next(error)
  }
})

app.post('/api/send', requireApiKey, async (request, response, next) => {
  try {
    const result = await whatsapp.sendStandardMessage(request.body ?? {})
    response.status(201).json({ ok: true, result })
  } catch (error) {
    next(error)
  }
})

app.post('/api/logout', requireApiKey, async (_request, response, next) => {
  try {
    response.json(await whatsapp.logout())
    whatsapp.start().catch((error) => console.error('Falha ao gerar novo QR Code:', error))
  } catch (error) {
    next(error)
  }
})

app.use((error, _request, response, _next) => {
  const statusCode = Number.isInteger(error.statusCode) ? error.statusCode : 500
  if (statusCode >= 500) console.error(error)
  response.status(statusCode).json({
    error: statusCode >= 500 ? 'Falha interna ao processar a solicitacao.' : error.message
  })
})

app.listen(config.port, config.host, () => {
  console.log(`Nexo Flow Baileys: http://${config.host}:${config.port}`)
})

whatsapp.start().catch((error) => {
  console.error('Falha ao iniciar o WhatsApp:', error)
})
