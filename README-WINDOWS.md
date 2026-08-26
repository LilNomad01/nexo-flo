# Nexo Flow Desktop — Windows

## Integração local com WhatsApp (Baileys)

Este repositório agora inclui um gateway local e independente para conectar o
WhatsApp por QR Code e enviar uma mensagem padrão a contatos autorizados. Ele
usa a biblioteca não oficial
[`@whiskeysockets/baileys`](https://github.com/WhiskeySockets/Baileys).

> O código-fonte do aplicativo desktop não estava presente neste repositório.
> Por isso, a integração foi adicionada como uma API local que pode rodar ao
> lado do executável e, futuramente, ser chamada pela interface principal.

### Requisitos

- Node.js 20 ou mais recente;
- WhatsApp instalado no celular;
- contatos que tenham autorizado o recebimento das mensagens.

### Iniciar

No Windows, dê dois cliques em `Iniciar Nexo Flow WhatsApp.cmd`. Na primeira
execução, o script cria o `.env`, instala as dependências e abre a tela local.

Se preferir iniciar manualmente, abra o PowerShell nesta pasta e execute:

```powershell
npm install
Copy-Item .env.example .env
npm start
```

Depois, abra [http://127.0.0.1:3001](http://127.0.0.1:3001), leia o QR Code em
**WhatsApp → Aparelhos conectados → Conectar aparelho** e preencha o formulário.

A sessão fica somente em `data/baileys-auth/` e esse diretório não é versionado.
Não compartilhe seu conteúdo: ele contém as chaves de acesso da conta.

### API

- `GET /api/status`: estado da conexão e QR Code atual;
- `POST /api/connect`: solicita conexão/reconexão;
- `POST /api/send`: envia uma mensagem;
- `POST /api/logout`: encerra a sessão e gera outro QR Code.

Exemplo de envio:

```powershell
$Body = @{
  phone = "5511999999999"
  name = "Maria"
  company = "Empresa Exemplo"
  consent = $true
} | ConvertTo-Json

Invoke-RestMethod `
  -Uri "http://127.0.0.1:3001/api/send" `
  -Method Post `
  -ContentType "application/json" `
  -Body $Body
```

O campo `message` é opcional. Sem ele, a API usa `DEFAULT_MESSAGE` do `.env`.
As variáveis `{{nome}}` e `{{empresa}}` são substituídas automaticamente.
Defina `API_KEY` no `.env` se a API for chamada por outro programa e envie a
mesma chave no header `X-API-Key`. Por segurança, mantenha `HOST=127.0.0.1`.

Há um intervalo mínimo entre envios e bloqueio de duplicatas acidentais. O
Baileys não é uma API oficial do WhatsApp; use-o de acordo com os termos da
plataforma e não envie spam.

## Executar

1. Descompacte o pacote.
2. Dê dois cliques em `Nexo Flow.exe`.
3. Os dados ficam em `%LOCALAPPDATA%\Nexo Flow`:
   - `data\nexoflow.db`: banco local;
   - `provider-secrets.json`: tokens cifrados;
   - `.env`: configuração;
   - `nexo-flow.log`: log técnico de inicialização.

O aplicativo usa o Microsoft Edge WebView2 presente no Windows 10/11. Nenhum terminal precisa permanecer aberto.

## Logs de disparo

Abra **Logs em tempo real** no menu. A tela atualiza a cada 2,5 segundos e mostra:

- início de cada tentativa;
- mensagem enviada e ID retornado pelo provedor;
- nova tentativa agendada;
- falha definitiva e motivo;
- status de entrega/leitura;
- mensagens e eventos recebidos;
- conexão e reconexão do stream UAZAPI.

## Eventos e webhooks

- **UAZAPI no Windows:** usa SSE direto e não exige URL pública. O receptor reconecta automaticamente.
- **UAZAPI hospedada:** também pode registrar o webhook HTTPS na tela **Diagnóstico de webhooks**.
- **Meta oficial:** exige uma `PUBLIC_BASE_URL` HTTPS estável, `WHATSAPP_VERIFY_TOKEN` e `WHATSAPP_APP_SECRET`. Configure esses valores em `.env`, reinicie o aplicativo e cadastre a callback exibida na tela no painel da Meta.

Para editar a configuração, execute `Configurar Nexo Flow Windows.cmd`.

## Gerar novamente o EXE

Com Python 3.9+ instalado, execute `Criar Aplicativo Windows.cmd`. O resultado será salvo em `dist\Nexo Flow.exe`.
