# Nexo Flow Web

Versão web hospedável reconstruída a partir do pacote **Nexo Flow Desktop Windows v1.3**, sem executar o binário. A interface original foi preservada e o runtime local (PyInstaller + WebView2 + SQLite no Windows) foi substituído por uma aplicação FastAPI que roda em Docker e aceita PostgreSQL.

## O que está incluído

- autenticação com sessão assinada, senha via PBKDF2-HMAC-SHA256, cookie `HttpOnly` e proteção CSRF;
- workspaces isolados por organização;
- contatos manuais e importação `.xlsx`/`.csv` com `;`, até 5.000 linhas;
- consentimento WhatsApp, opt-out automático por `PARAR`, supressão e frequency cap;
- listas e campanhas com revisão, confirmação explícita e fila idempotente;
- worker de envio com ritmo por campanha, retry exponencial e falha final;
- Meta WhatsApp Cloud API oficial;
- UAZAPI opcional e identificada como integração não oficial;
- inbox de conversas, webhooks assinados/deduplicados e logs ao vivo;
- SQLite para desenvolvimento e PostgreSQL para produção;
- Docker, Docker Compose, blueprint Render e configuração Railway.
- configuração Vercel com FastAPI serverless, PostgreSQL Neon e processamento autenticado da fila.

## Rodar localmente

Requer Python 3.11+.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env
uvicorn app.main:app --reload
```

Abra `http://localhost:8000`, crie o primeiro workspace e faça login.

Para testar a configuração completa com PostgreSQL:

```bash
export APP_SECRET="uma-chave-aleatoria-com-mais-de-32-caracteres"
docker compose up --build
```

## Hospedar

### Vercel

1. Vincule o diretório a um projeto com `vercel link`.
2. Conecte um PostgreSQL Neon pelo Vercel Marketplace; a integração fornece `DATABASE_URL`.
3. Configure `APP_ENV=production`, `APP_SECRET`, `SECURE_COOKIES=1`, `RUN_WORKER=0` e `ALLOW_REGISTRATION=0`.
4. Publique com `vercel deploy` e promova o deploy validado para produção.

Como funções serverless não mantêm um processo residente, as mensagens são processadas gradualmente enquanto a tela da campanha estiver aberta. Para envios totalmente desacompanhados em grande volume, conecte uma fila durável ou um serviço worker dedicado.

Mesmo com `ALLOW_REGISTRATION=0`, uma instalação com banco vazio permite criar exatamente o primeiro workspace. Depois do primeiro usuário, a rota de cadastro é fechada automaticamente.

### Render

1. Envie esta pasta para um repositório Git.
2. No Render, escolha **New → Blueprint** e selecione o repositório. O `render.yaml` cria o web service e o PostgreSQL.
3. Defina `PUBLIC_BASE_URL` como a URL HTTPS final, por exemplo `https://nexo-flow.onrender.com`.
4. Defina `WHATSAPP_VERIFY_TOKEN` e `WHATSAPP_APP_SECRET`.
5. Após o primeiro cadastro, altere `ALLOW_REGISTRATION=0` caso o sistema seja de uso privado.

O Blueprint usa a instância web gratuita para facilitar a primeira publicação. Como ela pode suspender por inatividade, troque para uma instância sempre ativa antes de operar campanhas contínuas.

### Railway

1. Crie um projeto a partir do repositório.
2. Adicione PostgreSQL e associe a variável `DATABASE_URL` ao serviço.
3. Configure `APP_ENV=production`, `APP_SECRET`, `PUBLIC_BASE_URL` e `SECURE_COOKIES=1`.
4. A Railway detecta o `Dockerfile` automaticamente. Nas configurações do serviço, defina o health check como `/api/health`.

### Qualquer VPS ou provedor Docker

Use o `Dockerfile`. Em produção, forneça uma URL PostgreSQL em `DATABASE_URL` e mantenha uma única instância com `RUN_WORKER=1`. Para escalar horizontalmente, execute o web app com `RUN_WORKER=0` e mantenha um único processo worker dedicado antes de aumentar as réplicas.

## Configurar a Meta Cloud API

1. Crie/configure seu app no Meta for Developers e obtenha WABA ID, Phone Number ID e access token.
2. No Nexo Flow, abra **Números WhatsApp** e valide o número.
3. Na Meta, configure o callback:

   `https://SEU-DOMINIO/api/webhooks/whatsapp`

4. Use o mesmo valor de `WHATSAPP_VERIFY_TOKEN` da hospedagem.
5. Defina `WHATSAPP_APP_SECRET`; em produção o endpoint rejeita notificações sem assinatura válida.

O token informado na tela é cifrado no banco com uma chave derivada de `APP_SECRET`. Trocar `APP_SECRET` invalida sessões e impede decifrar tokens já salvos; planeje a rotação.

## UAZAPI

A UAZAPI é não oficial. A versão hospedada usa webhook HTTPS em vez do SSE direto do aplicativo Windows. Depois de conectar a instância, abra **Diagnóstico de webhooks** e clique em **Configurar webhook HTTPS**. O callback usa um segredo individual por instância e deduplicação de eventos.

## Segurança e operação

- Use sempre HTTPS, `APP_SECRET` forte e PostgreSQL com backup.
- Depois do primeiro cadastro, considere `ALLOW_REGISTRATION=0`.
- Não compartilhe tokens em logs; os detalhes persistidos são sanitizados.
- O sistema exige opt-in e aplica limite de mensagens por contato em 7 dias.
- Comece com um número de teste oficial antes de ativar uma campanha real.
- `/api/health` verifica aplicação e banco; `/api/docs` expõe a documentação técnica da API.

## Testes

```bash
pytest
python -m compileall -q app
```

## Estrutura

```text
app/
  main.py              rotas web/API e ciclo da aplicação
  models.py            modelo relacional
  services.py          elegibilidade, variáveis e enfileiramento
  worker.py            envio, retry e conclusão de campanhas
  event_processor.py   eventos Meta/UAZAPI, inbox e opt-out
  providers/           clientes HTTP dos provedores
  templates/           interface recuperada e adaptada para web
  static/              estilos da interface
```

O ZIP e o executável originais não são necessários na hospedagem e não foram incluídos neste projeto.
