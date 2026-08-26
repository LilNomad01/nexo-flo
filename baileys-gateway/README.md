# Nexo Flow Baileys Gateway

Serviço Node.js persistente que mantém as sessões WebSocket do Baileys e oferece uma API HTTPS privada ao Nexo Flow.

## Variáveis

- `BAILEYS_GATEWAY_TOKEN`: segredo aleatório com no mínimo 32 caracteres.
- `BAILEYS_AUTH_DIR`: diretório persistente das sessões (padrão `/data/sessions` no Docker).
- `PORT`: porta HTTP (padrão `8080`).
- `LOG_LEVEL`: nível do Pino (padrão `info`).

## Hospedagem

Use um serviço Node/Docker com processo contínuo e volume persistente, como Railway, Render ou Fly.io. Exponha somente HTTPS e configure um volume em `/data`. A aplicação web na Vercel não armazena as credenciais do WhatsApp; ela guarda apenas a URL e o token cifrado deste gateway.

```bash
docker build -t nexo-baileys .
docker run --rm -p 8080:8080 -v nexo-baileys:/data \
  -e BAILEYS_GATEWAY_TOKEN='gere-um-segredo-com-32-caracteres' nexo-baileys
```

O health check público é `GET /health`. Todas as rotas `/sessions/*` exigem `Authorization: Bearer <token>`.
