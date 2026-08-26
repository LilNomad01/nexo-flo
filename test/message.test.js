import assert from 'node:assert/strict'
import test from 'node:test'
import { assertMessage, normalizePhone, renderMessage } from '../src/message.js'

test('normaliza telefone mantendo DDI e DDD', () => {
  assert.equal(normalizePhone('+55 (11) 99999-9999'), '5511999999999')
})

test('recusa telefone incompleto', () => {
  assert.throws(() => normalizePhone('9999-9999'), /DDI e DDD/)
})

test('substitui variaveis da mensagem', () => {
  assert.equal(
    renderMessage('Ola {{ nome }} da {{empresa}}!', { name: 'Ana', company: 'Nexo' }),
    'Ola Ana da Nexo!'
  )
})

test('usa valores neutros quando dados opcionais nao existem', () => {
  assert.equal(renderMessage('Ola {{nome}}, tudo bem? - {{empresa}}'), 'Ola, tudo bem? - sua empresa')
})

test('recusa mensagem vazia ou acima do limite', () => {
  assert.throws(() => assertMessage(''), /vazia/)
  assert.throws(() => assertMessage('x'.repeat(4097)), /4096/)
})
