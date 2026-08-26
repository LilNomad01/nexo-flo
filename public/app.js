const elements = {
  statusDot: document.querySelector('#status-dot'),
  statusLabel: document.querySelector('#status-label'),
  statusDetail: document.querySelector('#status-detail'),
  qrImage: document.querySelector('#qr-image'),
  qrPlaceholder: document.querySelector('#qr-placeholder'),
  sendForm: document.querySelector('#send-form'),
  sendButton: document.querySelector('#send-button'),
  logoutButton: document.querySelector('#logout-button'),
  feedback: document.querySelector('#feedback')
}

let connected = false

const labels = {
  starting: ['Iniciando...', 'Preparando a conexão com o WhatsApp.'],
  connecting: ['Conectando...', 'Aguarde enquanto criamos uma sessão segura.'],
  qr_ready: ['Leia o QR Code', 'Use o WhatsApp do celular para conectar este computador.'],
  connected: ['WhatsApp conectado', 'A sessão ficará salva somente neste computador.'],
  reconnecting: ['Reconectando...', 'A conexão caiu e será restabelecida automaticamente.'],
  logged_out: ['Sessão encerrada', 'Aguarde a geração de um novo QR Code.'],
  error: ['Falha na conexão', 'Verifique o terminal e tente iniciar novamente.']
}

function renderStatus(status) {
  connected = status.connected
  const [label, detail] = labels[status.status] || labels.error
  elements.statusLabel.textContent = label
  elements.statusDetail.textContent = status.error || detail
  elements.statusDot.className = `status-dot ${status.connected ? 'connected' : ''} ${status.status === 'error' ? 'error' : ''}`
  elements.sendButton.disabled = !status.connected
  elements.logoutButton.classList.toggle('hidden', !status.connected)

  if (status.qrDataUrl) {
    elements.qrImage.src = status.qrDataUrl
    elements.qrImage.classList.remove('hidden')
    elements.qrPlaceholder.classList.add('hidden')
  } else {
    elements.qrImage.removeAttribute('src')
    elements.qrImage.classList.add('hidden')
    elements.qrPlaceholder.classList.remove('hidden')
    elements.qrPlaceholder.textContent = status.connected
      ? `Conectado${status.phone ? `: +${status.phone}` : ''}`
      : 'O QR Code aparecerá aqui.'
  }
}

async function refreshStatus() {
  try {
    const response = await fetch('/api/status', { cache: 'no-store' })
    renderStatus(await response.json())
  } catch {
    renderStatus({ status: 'error', connected: false, error: 'A API local não está respondendo.' })
  }
}

elements.sendForm.addEventListener('submit', async (event) => {
  event.preventDefault()
  if (!connected) return

  const form = new FormData(elements.sendForm)
  elements.sendButton.disabled = true
  elements.feedback.className = ''
  elements.feedback.textContent = 'Enviando...'

  try {
    const response = await fetch('/api/send', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({
        phone: form.get('phone'),
        name: form.get('name'),
        company: form.get('company'),
        message: form.get('message'),
        consent: form.get('consent') === 'on'
      })
    })
    const body = await response.json()
    if (!response.ok) throw new Error(body.error || 'Não foi possível enviar.')

    elements.feedback.className = 'success'
    elements.feedback.textContent = `Mensagem enviada às ${new Date(body.result.sentAt).toLocaleTimeString('pt-BR')}.`
  } catch (error) {
    elements.feedback.className = 'error'
    elements.feedback.textContent = error.message
  } finally {
    elements.sendButton.disabled = !connected
  }
})

elements.logoutButton.addEventListener('click', async () => {
  if (!window.confirm('Desconectar este WhatsApp e gerar um novo QR Code?')) return
  elements.logoutButton.disabled = true
  try {
    const response = await fetch('/api/logout', { method: 'POST' })
    if (!response.ok) throw new Error('Não foi possível encerrar a sessão.')
    await refreshStatus()
  } catch (error) {
    elements.feedback.className = 'error'
    elements.feedback.textContent = error.message
  } finally {
    elements.logoutButton.disabled = false
  }
})

refreshStatus()
setInterval(refreshStatus, 1500)

