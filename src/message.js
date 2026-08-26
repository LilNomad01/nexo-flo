export function normalizePhone(value) {
  const digits = String(value ?? '').replace(/\D/g, '')

  if (digits.length < 10 || digits.length > 15) {
    throw new Error('Informe o telefone com DDI e DDD, por exemplo: 5511999999999.')
  }

  return digits
}

export function renderMessage(template, variables = {}) {
  const replacements = {
    nome: String(variables.name ?? '').trim(),
    empresa: String(variables.company ?? '').trim() || 'sua empresa'
  }

  return String(template ?? '')
    .replace(/{{\s*(nome|empresa)\s*}}/gi, (_, key) => replacements[key.toLowerCase()])
    .replace(/\s+([,.;!?])/g, '$1')
    .trim()
}

export function assertMessage(message) {
  if (!message) {
    throw new Error('A mensagem nao pode ficar vazia.')
  }

  if (message.length > 4096) {
    throw new Error('A mensagem ultrapassa o limite de 4096 caracteres.')
  }
}
