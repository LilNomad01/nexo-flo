# Nexo Flow Desktop — Windows

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
