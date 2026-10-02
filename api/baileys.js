import { createCipheriv, createDecipheriv, createHash, createHmac, randomBytes, timingSafeEqual } from 'node:crypto'

import { waitUntil } from '@vercel/functions'
import makeWASocket, { Browsers, BufferJSON, DisconnectReason, fetchLatestWaWebVersion, initAuthCreds, proto } from 'baileys'
import QRCode from 'qrcode'
import pg from 'pg'
import pino from 'pino'

export const maxDuration = 300

const databaseUrl = process.env.DATABASE_URL || process.env.POSTGRES_URL
const appSecret = process.env.APP_SECRET || ''
const logger = pino({ level: process.env.BAILEYS_LOG_LEVEL || 'info' })
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

function extractHttpUrls(value) {
  const matches = String(value || '').match(/https?:\/\/[^\s<>"']+/gi) || []
  return matches
    .map(url => url.replace(/[),.!?;:]+$/, ''))
    .filter(Boolean)
}

function linkPreviewFor(url) {
  if (!url) return undefined

  let title = 'Abrir link'
  let description = 'Toque para abrir.'

  try {
    const parsed = new URL(url)
    title = parsed.hostname.replace(/^www\./, '')

    if (parsed.hostname === 'chat.whatsapp.com') {
      title = 'Convite para grupo do WhatsApp'
      description = 'Toque para abrir o convite.'
    }
  } catch {}

  return {
    'matched-text': url,
    'canonical-url': url,
    title,
    description,
    previewType: 0,
  }
}

function captionWithoutUrls(text, urls) {
  let caption = String(text || '')

  for (const url of urls) {
    caption = caption.replace(url, '')
  }

  return caption
    .replace(/[ \t]+\n/g, '\n')
    .replace(/\n{3,}/g, '\n\n')
    .trim()
}

function buildOutboundContent(textValue, mediaValue) {
  const text = String(textValue || '').trim()
  const media =
    mediaValue && typeof mediaValue === 'object'
      ? mediaValue
      : null

  if (!text && !media) {
    const error = new Error('A mensagem está vazia.')
    error.status = 422
    throw error
  }

  const urls = extractHttpUrls(text)

  if (!media) {
    const firstUrl = urls[0]
    return {
      content: firstUrl
        ? { text, linkPreview: linkPreviewFor(firstUrl) }
        : { text },
      extraLinkText: null,
    }
  }

  const mediaType = String(media.type || '')
  const mime = String(media.mime || '')
  const filename = String(media.filename || 'arquivo').slice(0, 220)
  const rawData = String(media.data || '')
  const mediaCaption = urls.length
    ? captionWithoutUrls(text, urls)
    : text

  const allowedTypes = new Set([
    'image',
    'video',
    'document',
  ])

  const allowedMime = new Set([
    'image/jpeg',
    'image/png',
    'image/webp',
    'video/mp4',
    'application/pdf',
  ])

  if (!allowedTypes.has(mediaType) || !allowedMime.has(mime)) {
    const error = new Error('Formato de mídia não suportado.')
    error.status = 422
    throw error
  }

  const mediaBuffer = Buffer.from(rawData, 'base64')

  if (!mediaBuffer.length || mediaBuffer.length > 2500000) {
    const error = new Error('A mídia precisa ter até 2,5 MB.')
    error.status = 422
    throw error
  }

  let content

  if (mediaType === 'image') {
    content = {
      image: mediaBuffer,
      mimetype: mime,
      caption: mediaCaption || undefined,
    }
  } else if (mediaType === 'video') {
    content = {
      video: mediaBuffer,
      mimetype: mime,
      caption: mediaCaption || undefined,
    }
  } else {
    content = {
      document: mediaBuffer,
      mimetype: mime,
      fileName: filename || 'documento.pdf',
      caption: mediaCaption || undefined,
    }
  }

  return {
    content,
    extraLinkText: urls.length ? urls.join('\n') : null,
    firstUrl: urls[0] || null,
  }
}

const sleep = milliseconds =>
  new Promise(resolve => setTimeout(resolve, milliseconds))

async function claimCampaignDrainLease(
  campaignId,
  workspaceId,
  sessionId,
) {
  await ensureSchema()

  const result = await pool.query(
    `
    INSERT INTO baileys_campaign_drains (
      campaign_id,
      workspace_id,
      session_id,
      lease_until,
      updated_at
    )
    VALUES (
      $1,
      $2,
      $3,
      now() + interval '280 seconds',
      now()
    )
    ON CONFLICT (campaign_id)
    DO UPDATE SET
      workspace_id = EXCLUDED.workspace_id,
      session_id = EXCLUDED.session_id,
      lease_until = EXCLUDED.lease_until,
      updated_at = now()
    WHERE
      baileys_campaign_drains.lease_until <= now()
    RETURNING campaign_id
    `,
    [
      campaignId,
      workspaceId,
      sessionId,
    ],
  )

  return result.rowCount === 1
}

async function refreshCampaignDrainLease(
  campaignId,
  sessionId,
) {
  await pool.query(
    `
    UPDATE baileys_campaign_drains
    SET
      lease_until = now() + interval '280 seconds',
      updated_at = now()
    WHERE
      campaign_id = $1
      AND session_id = $2
    `,
    [
      campaignId,
      sessionId,
    ],
  )
}

async function releaseCampaignDrainLease(
  campaignId,
  sessionId,
) {
  await pool.query(
    `
    DELETE FROM baileys_campaign_drains
    WHERE
      campaign_id = $1
      AND session_id = $2
    `,
    [
      campaignId,
      sessionId,
    ],
  )
}

async function claimCampaignBatch(
  campaignId,
  workspaceId,
  sessionId,
  limit = 15,
) {
  const client = await pool.connect()

  try {
    await client.query('BEGIN')

    await client.query(
      `
      UPDATE outbox_jobs AS job
      SET
        status = 'pending',
        locked_at = NULL,
        available_at = now(),
        last_error = NULL
      FROM messages AS message
      WHERE
        job.message_id = message.id
        AND job.workspace_id = $2
        AND message.campaign_id = $1
        AND job.status = 'processing'
        AND job.locked_at IS NOT NULL
        AND job.locked_at <= now() - interval '75 seconds'
      `,
      [campaignId, workspaceId],
    )

    const selected = await client.query(
      `
      SELECT
        job.id AS job_id,
        job.attempts,
        job.idempotency_key,
        message.id AS message_id,
        message.body,
        message.type,
        message.contact_id,
        message.campaign_step_id,
        contact.phone_e164,
        step.position,
        step.media_type,
        step.media_mime,
        step.media_filename,
        step.media_data_base64
      FROM outbox_jobs AS job
      JOIN messages AS message
        ON message.id = job.message_id
      JOIN campaigns AS campaign
        ON campaign.id = message.campaign_id
      JOIN whatsapp_phone_numbers AS number
        ON number.id = campaign.phone_number_id
      JOIN contacts AS contact
        ON contact.id = message.contact_id
      LEFT JOIN campaign_steps AS step
        ON step.id = message.campaign_step_id
      WHERE
        campaign.id = $1
        AND campaign.workspace_id = $2
        AND campaign.status = 'running'
        AND number.provider = 'baileys'
        AND number.waba_id = 'vercel-internal'
        AND number.phone_number_id = $3
        AND number.status = 'connected'
        AND job.status = 'pending'
        AND (
          job.available_at <= now()
          OR COALESCE(step.position, 1) = 1
        )
      ORDER BY
        CASE
          WHEN COALESCE(step.position, 1) = 1
            THEN 0
          ELSE 1
        END,
        job.available_at,
        job.created_at
      LIMIT $4
      FOR UPDATE OF job SKIP LOCKED
      `,
      [
        campaignId,
        workspaceId,
        sessionId,
        Math.max(1, Math.min(Number(limit) || 15, 15)),
      ],
    )

    const rows = selected.rows

    if (rows.length) {
      await client.query(
        `
        UPDATE outbox_jobs
        SET
          status = 'processing',
          attempts = attempts + 1,
          locked_at = now()
        WHERE id = ANY($1)
        `,
        [rows.map(row => row.job_id)],
      )
    }

    await client.query('COMMIT')

    return rows.map(row => ({
      ...row,
      attempt: Number(row.attempts || 0) + 1,
    }))
  } catch (error) {
    try {
      await client.query('ROLLBACK')
    } catch {}

    throw error
  } finally {
    client.release()
  }
}

async function campaignState(
  campaignId,
  workspaceId,
) {
  const result = await pool.query(
    `
    SELECT
      campaign.status,
      COUNT(*) FILTER (
        WHERE job.status IN ('pending', 'processing')
      )::int AS pending,
      COUNT(*) FILTER (
        WHERE job.status = 'failed'
      )::int AS failed,
      COUNT(*) FILTER (
        WHERE job.status = 'cancelled'
      )::int AS cancelled,
      MIN(job.available_at) FILTER (
        WHERE job.status = 'pending'
      ) AS next_available_at
    FROM campaigns AS campaign
    LEFT JOIN messages AS message
      ON message.campaign_id = campaign.id
    LEFT JOIN outbox_jobs AS job
      ON job.message_id = message.id
    WHERE
      campaign.id = $1
      AND campaign.workspace_id = $2
    GROUP BY campaign.id, campaign.status
    `,
    [campaignId, workspaceId],
  )

  return result.rows[0] || null
}

async function finishCampaignIfDone(
  campaignId,
  workspaceId,
) {
  const state = await campaignState(
    campaignId,
    workspaceId,
  )

  if (
    !state ||
    state.status !== 'running' ||
    Number(state.pending || 0) > 0
  ) {
    return state
  }

  let status = 'completed'

  if (Number(state.failed || 0) > 0) {
    status = 'failed'
  } else if (Number(state.cancelled || 0) > 0) {
    status = 'cancelled'
  }

  await pool.query(
    `
    UPDATE campaigns
    SET
      status = $3,
      completed_at = now()
    WHERE
      id = $1
      AND workspace_id = $2
      AND status = 'running'
    `,
    [
      campaignId,
      workspaceId,
      status,
    ],
  )

  return {
    ...state,
    status,
  }
}

async function markCampaignJobSent(
  row,
  messageId,
  campaignId,
) {
  const client = await pool.connect()

  try {
    await client.query('BEGIN')

    await client.query(
      `
      UPDATE messages
      SET
        status = 'sent',
        provider_message_id = $2,
        sent_at = now(),
        error_message = NULL
      WHERE id = $1
      `,
      [
        row.message_id,
        messageId || null,
      ],
    )

    await client.query(
      `
      UPDATE outbox_jobs
      SET
        status = 'sent',
        locked_at = NULL,
        last_error = NULL
      WHERE id = $1
      `,
      [row.job_id],
    )

    await client.query(
      `
      UPDATE campaign_recipients
      SET
        status = 'sent',
        reason = NULL
      WHERE
        campaign_id = $1
        AND contact_id = $2
      `,
      [
        campaignId,
        row.contact_id,
      ],
    )

    await client.query('COMMIT')
  } catch (error) {
    try {
      await client.query('ROLLBACK')
    } catch {}

    throw error
  } finally {
    client.release()
  }
}

async function markCampaignJobFailed(
  row,
  campaignId,
  message,
  permanent = false,
) {
  const finalFailure =
    permanent ||
    Number(row.attempt || 1) >= 4

  const client = await pool.connect()

  try {
    await client.query('BEGIN')

    if (finalFailure) {
      await client.query(
        `
        UPDATE messages
        SET
          status = 'failed',
          error_message = $2
        WHERE id = $1
        `,
        [
          row.message_id,
          String(message || 'Falha no envio').slice(0, 1000),
        ],
      )

      await client.query(
        `
        UPDATE outbox_jobs
        SET
          status = 'failed',
          locked_at = NULL,
          last_error = $2
        WHERE id = $1
        `,
        [
          row.job_id,
          String(message || 'Falha no envio').slice(0, 1000),
        ],
      )

      await client.query(
        `
        UPDATE campaign_recipients
        SET
          status = 'failed',
          reason = $3
        WHERE
          campaign_id = $1
          AND contact_id = $2
        `,
        [
          campaignId,
          row.contact_id,
          String(message || 'Falha no envio').slice(0, 160),
        ],
      )
    } else {
      await client.query(
        `
        UPDATE messages
        SET
          status = 'queued',
          error_message = $2
        WHERE id = $1
        `,
        [
          row.message_id,
          String(message || 'Falha temporária').slice(0, 1000),
        ],
      )

      await client.query(
        `
        UPDATE outbox_jobs
        SET
          status = 'pending',
          locked_at = NULL,
          last_error = $2,
          available_at = now() + interval '3 seconds'
        WHERE id = $1
        `,
        [
          row.job_id,
          String(message || 'Falha temporária').slice(0, 1000),
        ],
      )
    }

    await client.query('COMMIT')
  } catch (error) {
    try {
      await client.query('ROLLBACK')
    } catch {}

    throw error
  } finally {
    client.release()
  }
}

async function sendCampaignRow(
  handle,
  row,
  campaignId,
  existingMessageId = null,
  jid = null,
) {
  if (existingMessageId) {
    await markCampaignJobSent(
      row,
      existingMessageId,
      campaignId,
    )

    return {
      ok: true,
      duplicate: true,
    }
  }

  if (!jid) {
    await markCampaignJobFailed(
      row,
      campaignId,
      'O número do destinatário não está cadastrado no WhatsApp.',
      true,
    )

    return {
      ok: false,
      permanent: true,
    }
  }

  try {
    const media =
      row.media_type
      && row.media_mime
      && row.media_data_base64
        ? {
            type: row.media_type,
            mime: row.media_mime,
            filename: row.media_filename || 'arquivo',
            data: row.media_data_base64,
          }
        : null

    const outbound = buildOutboundContent(
      row.body || '',
      media,
    )

    const sent = await handle.sock.sendMessage(
      jid,
      outbound.content,
    )

    let messageId = sent?.key?.id

    if (outbound.extraLinkText) {
      const linkSent = await handle.sock.sendMessage(
        jid,
        {
          text: outbound.extraLinkText,
          linkPreview: linkPreviewFor(
            outbound.firstUrl
          ),
        },
      )

      messageId =
        linkSent?.key?.id ||
        messageId
    }

    if (!messageId) {
      throw new Error(
        'O WhatsApp não retornou o ID da mensagem.'
      )
    }

    await pool.query(
      `
      INSERT INTO baileys_sent_requests (
        session_id,
        request_id,
        message_id
      )
      VALUES ($1,$2,$3)
      ON CONFLICT (session_id, request_id)
      DO NOTHING
      `,
      [
        handle.sessionId,
        row.idempotency_key,
        messageId,
      ],
    )

    await markCampaignJobSent(
      row,
      messageId,
      campaignId,
    )

    return {
      ok: true,
      id: messageId,
    }
  } catch (error) {
    await markCampaignJobFailed(
      row,
      campaignId,
      error?.message || 'Falha ao enviar a mensagem.',
      false,
    )

    return {
      ok: false,
      error: String(error),
    }
  }
}

async function runCampaignDrain(
  sessionId,
  campaignId,
  workspaceId,
) {
  return withSessionLock(
    sessionId,
    async () => {
      const handle = await openSocket(
        sessionId,
        260_000,
      )

      // Usado por sendCampaignRow para gravar idempotência.
      handle.sessionId = sessionId

      let processed = 0
      const deadline = Date.now() + 235_000

      try {
        await waitForOpen(
          handle,
          30_000,
        )

        while (Date.now() < deadline) {
          const state = await campaignState(
            campaignId,
            workspaceId,
          )

          if (
            !state ||
            state.status !== 'running'
          ) {
            return {
              processed,
              hasMore: false,
              status:
                state?.status ||
                'missing',
            }
          }

          const rows = await claimCampaignBatch(
            campaignId,
            workspaceId,
            sessionId,
            15,
          )

          await refreshCampaignDrainLease(
            campaignId,
            sessionId,
          )

          if (!rows.length) {
            const refreshed =
              await finishCampaignIfDone(
                campaignId,
                workspaceId,
              )

            if (
              !refreshed ||
              refreshed.status !== 'running'
            ) {
              return {
                processed,
                hasMore: false,
                status:
                  refreshed?.status ||
                  'completed',
              }
            }

            const nextAt =
              refreshed.next_available_at
                ? new Date(
                    refreshed.next_available_at
                  ).getTime()
                : Date.now() + 700

            const waitMs = Math.max(
              250,
              Math.min(
                1500,
                nextAt - Date.now(),
              ),
            )

            await sleep(waitMs)
            continue
          }

          const requestIds =
            rows.map(
              row => row.idempotency_key
            )

          const existing = await pool.query(
            `
            SELECT
              request_id,
              message_id
            FROM baileys_sent_requests
            WHERE
              session_id = $1
              AND request_id = ANY($2)
            `,
            [
              sessionId,
              requestIds,
            ],
          )

          const existingByRequest =
            new Map(
              existing.rows.map(row => [
                row.request_id,
                row.message_id,
              ])
            )

          const numbers = [
            ...new Set(
              rows
                .filter(
                  row =>
                    !existingByRequest.has(
                      row.idempotency_key
                    )
                )
                .map(
                  row =>
                    String(
                      row.phone_e164 || ''
                    ).replace(/\D/g, '')
                )
                .filter(Boolean)
            ),
          ]

          const checked =
            numbers.length
              ? await handle.sock.onWhatsApp(
                  ...numbers
                )
              : []

          const jidByNumber = new Map()

          for (const item of checked) {
            const number =
              String(item?.jid || '')
                .split('@', 1)[0]

            if (
              item?.exists &&
              number
            ) {
              jidByNumber.set(
                number,
                item.jid,
              )
            }
          }

          for (const row of rows) {
            const current = await campaignState(
              campaignId,
              workspaceId,
            )

            if (
              !current ||
              current.status !== 'running'
            ) {
              await pool.query(
                `
                UPDATE outbox_jobs
                SET
                  status = $2,
                  locked_at = NULL
                WHERE id = $1
                `,
                [
                  row.job_id,
                  current?.status === 'paused'
                    ? 'paused'
                    : 'cancelled',
                ],
              )

              continue
            }

            const normalizedNumber =
              String(
                row.phone_e164 || ''
              ).replace(/\D/g, '')

            await sendCampaignRow(
              handle,
              row,
              campaignId,
              existingByRequest.get(
                row.idempotency_key
              ) || null,
              jidByNumber.get(
                normalizedNumber
              ) || null,
            )

            processed += 1

            await sleep(300)
          }

          await handle.flush()
        }

        const state = await campaignState(
          campaignId,
          workspaceId,
        )

        return {
          processed,
          hasMore:
            Boolean(
              state
              && state.status === 'running'
              && Number(state.pending || 0) > 0
            ),
          status:
            state?.status ||
            'missing',
        }
      } finally {
        try {
          await handle.flush()
        } catch {}

        handle.close()
      }
    },
  )
}

async function triggerCampaignDrain(
  baseUrl,
  sessionId,
  campaignId,
  workspaceId,
) {
  const action = 'drain-campaign'
  const body = {
    campaignId,
    workspaceId,
  }
  const canonical = stableJson(body)
  const timestamp = String(
    Math.floor(Date.now() / 1000)
  )
  const signature = createHmac(
    'sha256',
    appSecret,
  )
    .update(
      `${timestamp}.${action}.${sessionId}.${canonical}`
    )
    .digest('hex')

  return fetch(
    `${baseUrl}/baileys-internal?action=${action}&sessionId=${encodeURIComponent(sessionId)}`,
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Nexo-Timestamp': timestamp,
        'X-Nexo-Signature': signature,
      },
      body: canonical,
    },
  )
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
      CREATE TABLE IF NOT EXISTS baileys_campaign_drains (
        campaign_id varchar(48) PRIMARY KEY,
        workspace_id varchar(48) NOT NULL,
        session_id varchar(80) NOT NULL,
        lease_until timestamptz NOT NULL,
        updated_at timestamptz NOT NULL DEFAULT now()
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
  let qrcode_svg = null

  try {
    qrcode = row.qr_payload ? decrypt(row.qr_payload) : null
  } catch {
    qrcode = null
  }

  if (qrcode) {
    try {
      qrcode_svg = await QRCode.toString(qrcode, {
        type: 'svg',
        errorCorrectionLevel: 'M',
        margin: 3,
        width: 320,
      })
    } catch (error) {
      console.error('[Baileys] QR SVG generation failed', {
        sessionId,
        error: String(error),
      })
    }
  }

  return {
    sessionId,
    status: row.status,
    connected: row.status === 'connected',
    phone: row.phone,
    qrcode,
    qrcode_svg,
    lastError: row.last_error,
  }
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

async function withSessionLock(sessionId, callback) {
  const client = await pool.connect()
  // v2 intentionally changes the key so locks left behind by an older
  // serverless invocation cannot keep the current queue blocked.
  const lockName = `baileys:v2:${sessionId}`
  let locked = false
  let transactionOpen = false

  try {
    await client.query('BEGIN')
    transactionOpen = true

    const deadline = Date.now() + 8_000

    while (!locked && Date.now() < deadline) {
      const result = await client.query(
        'SELECT pg_try_advisory_xact_lock(hashtext($1)::bigint) AS locked',
        [lockName],
      )

      locked = result.rows[0]?.locked === true

      if (!locked) {
        await new Promise(resolve => setTimeout(resolve, 350))
      }
    }

    if (!locked) {
      const error = new Error(
        'A sessão Baileys está ocupada com o envio anterior. Tentaremos novamente automaticamente.'
      )
      error.status = 429
      throw error
    }

    const result = await callback()
    await client.query('COMMIT')
    transactionOpen = false
    return result
  } catch (error) {
    if (transactionOpen) {
      try {
        await client.query('ROLLBACK')
      } catch {}
      transactionOpen = false
    }
    throw error
  } finally {
    if (transactionOpen) {
      try {
        await client.query('ROLLBACK')
      } catch {}
    }
    client.release()
  }
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

    const { version, isLatest } = await fetchLatestWaWebVersion()

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
      qrTimeout: 120_000,
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

    flush: async () => {
      await saveCreds()
    },

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

async function handleAction(action, sessionId, body, request) {
  if (action === 'status') return sessionSnapshot(sessionId)

  if (action === 'disconnect') {
    await ensureSchema()
    await clearAuth(sessionId)
    await upsertSession(sessionId, {
      status: 'disconnected',
      qrcode: null,
      lastError: null,
      generationId: null,
    })
    return sessionSnapshot(sessionId)
  }

  if (action === 'connect' || action === 'create') {
    if (action === 'connect') await clearAuth(sessionId)
    const generationId = randomBytes(16).toString('hex')
    await upsertSession(sessionId, { status: 'connecting', qrcode: null, lastError: null, generationId })
    const handle = await openSocket(sessionId, 240_000, generationId)
    const first = await Promise.race([handle.first, new Promise(resolve => setTimeout(() => resolve({ kind: 'timeout' }), 90_000))])
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
  if (action === 'drain-campaign') {
    const campaignId = String(
      body.campaignId || ''
    ).trim()
    const workspaceId = String(
      body.workspaceId || ''
    ).trim()

    if (!campaignId || !workspaceId) {
      const error = new Error(
        'Campanha ou workspace inválido.'
      )
      error.status = 422
      throw error
    }

    const claimed =
      await claimCampaignDrainLease(
        campaignId,
        workspaceId,
        sessionId,
      )

    if (!claimed) {
      return {
        started: false,
        alreadyRunning: true,
        campaignId,
      }
    }

    const host = String(
      request.headers.host || ''
    )

    const baseUrl =
      process.env.VERCEL_PROJECT_PRODUCTION_URL
        ? `https://${process.env.VERCEL_PROJECT_PRODUCTION_URL}`
        : `https://${host}`

    waitUntil(
      (async () => {
        let continueDrain = false

        try {
          const result =
            await runCampaignDrain(
              sessionId,
              campaignId,
              workspaceId,
            )

          continueDrain =
            Boolean(result.hasMore)

          console.info(
            '[Baileys] campaign drain finished',
            {
              sessionId,
              campaignId,
              processed:
                result.processed,
              hasMore:
                result.hasMore,
              status:
                result.status,
            },
          )
        } catch (error) {
          const message =
            String(error || '')

          console.error(
            '[Baileys] campaign drain failed',
            {
              sessionId,
              campaignId,
              error: message,
            },
          )

          const normalized =
            message.toLowerCase()

          const authError =
            normalized.includes('pareada pelo qr')
            || normalized.includes('pareado pelo qr')
            || normalized.includes('logged out')
            || normalized.includes('sessão removida')
            || normalized.includes('sessao removida')

          if (authError) {
            await pool.query(
              `
              UPDATE campaigns
              SET status = 'paused'
              WHERE
                id = $1
                AND workspace_id = $2
                AND status = 'running'
              `,
              [
                campaignId,
                workspaceId,
              ],
            )
          } else {
            continueDrain = true
          }
        } finally {
          await releaseCampaignDrainLease(
            campaignId,
            sessionId,
          )
        }

        if (continueDrain) {
          await sleep(800)

          try {
            await triggerCampaignDrain(
              baseUrl,
              sessionId,
              campaignId,
              workspaceId,
            )
          } catch (retryError) {
            console.error(
              '[Baileys] campaign drain continuation failed',
              {
                sessionId,
                campaignId,
                error:
                  String(
                    retryError || ''
                  ),
              },
            )
          }
        }
      })(),
    )

    return {
      started: true,
      alreadyRunning: false,
      campaignId,
    }
  }

  if (action === 'batch-messages') {
    return withSessionLock(sessionId, async () => {
      await ensureSchema()

      const input = Array.isArray(body.messages)
        ? body.messages.slice(0, 15)
        : []

      if (!input.length) {
        const error = new Error('Nenhuma mensagem foi enviada no lote.')
        error.status = 422
        throw error
      }

      const normalized = input.map((item, index) => {
        const requestId = String(item?.requestId || '').trim()
        const to = String(item?.to || '').replace(/\D/g, '')

        if (!requestId || !to) {
          const error = new Error(
            'Mensagem ' + (index + 1) + ' possui destinatário ou requestId inválido.'
          )
          error.status = 422
          throw error
        }

        return {
          requestId,
          to,
          text: String(item?.text || ''),
          media:
            item?.media && typeof item.media === 'object'
              ? item.media
              : null,
        }
      })

      const requestIds = normalized.map(item => item.requestId)

      const existing = await pool.query(
        'SELECT request_id, message_id FROM baileys_sent_requests WHERE session_id=$1 AND request_id = ANY($2)',
        [sessionId, requestIds],
      )

      const existingByRequest = new Map(
        existing.rows.map(row => [
          row.request_id,
          row.message_id,
        ])
      )

      const pending = normalized.filter(
        item => !existingByRequest.has(item.requestId)
      )

      const results = normalized
        .filter(item => existingByRequest.has(item.requestId))
        .map(item => ({
          requestId: item.requestId,
          ok: true,
          id: existingByRequest.get(item.requestId),
          duplicate: true,
        }))

      if (!pending.length) {
        return {
          results,
          sent: results.length,
          failed: 0,
        }
      }

      const handle = await openSocket(
        sessionId,
        70_000,
      )

      try {
        await waitForOpen(
          handle,
          30_000,
        )

        const uniqueNumbers = [
          ...new Set(
            pending.map(item => item.to)
          ),
        ]

        const checked =
          uniqueNumbers.length
            ? await handle.sock.onWhatsApp(...uniqueNumbers)
            : []

        const jidByNumber = new Map()

        for (const item of checked) {
          const number =
            String(item?.jid || '')
              .split('@', 1)[0]

          if (item?.exists && number) {
            jidByNumber.set(number, item.jid)
          }
        }

        for (let index = 0; index < pending.length; index += 1) {
          const item = pending[index]
          const jid = jidByNumber.get(item.to)

          if (!jid) {
            results.push({
              requestId: item.requestId,
              ok: false,
              code: 'recipient_not_on_whatsapp',
              error: 'O número do destinatário não está cadastrado no WhatsApp.',
            })
            continue
          }

          try {
            const outbound = buildOutboundContent(
              item.text,
              item.media,
            )

            const sent = await handle.sock.sendMessage(
              jid,
              outbound.content,
            )

            let messageId = sent?.key?.id

            if (outbound.extraLinkText) {
              const linkSent = await handle.sock.sendMessage(
                jid,
                {
                  text: outbound.extraLinkText,
                  linkPreview: linkPreviewFor(
                    outbound.firstUrl
                  ),
                },
              )

              messageId =
                linkSent?.key?.id ||
                messageId
            }

            if (!messageId) {
              throw new Error(
                'O WhatsApp não retornou o ID da mensagem.'
              )
            }

            await pool.query(
              'INSERT INTO baileys_sent_requests (session_id, request_id, message_id) VALUES ($1,$2,$3) ON CONFLICT (session_id, request_id) DO NOTHING',
              [
                sessionId,
                item.requestId,
                messageId,
              ],
            )

            results.push({
              requestId: item.requestId,
              ok: true,
              id: messageId,
            })
          } catch (error) {
            results.push({
              requestId: item.requestId,
              ok: false,
              code: String(error?.status || 'send_failed'),
              error:
                error?.message ||
                'Falha ao enviar a mensagem.',
            })
          }

          // Mantém o socket rápido sem criar uma rajada instantânea.
          if (index < pending.length - 1) {
            await new Promise(
              resolve => setTimeout(resolve, 350)
            )
          }
        }

        await handle.flush()

        await upsertSession(
          sessionId,
          {
            status: 'connected',
            qrcode: null,
            lastError: null,
          },
        )

        return {
          results,
          sent: results.filter(item => item.ok).length,
          failed: results.filter(item => !item.ok).length,
        }
      } finally {
        try {
          await handle.flush()
        } catch {}

        handle.close()
      }
    })
  }

  if (action === 'messages') {
    return withSessionLock(sessionId, async () => {
      await ensureSchema()

      const existing = await pool.query(
        'SELECT message_id FROM baileys_sent_requests WHERE session_id=$1 AND request_id=$2',
        [sessionId, body.requestId],
      )

      if (existing.rows[0]) {
        return {
          id: existing.rows[0].message_id,
          duplicate: true,
        }
      }

      const handle = await openSocket(
        sessionId,
        70_000,
      )

      try {
        await waitForOpen(
          handle,
          35_000,
        )

        const to = String(
          body.to || ''
        ).replace(/\D/g, '')

        const text = String(
          body.text || ''
        ).trim()

        const media =
          body.media && typeof body.media === 'object'
            ? body.media
            : null

        if (!to) {
          const error = new Error(
            'Número do destinatário inválido.'
          )
          error.status = 422
          throw error
        }

        if (!text && !media) {
          const error = new Error(
            'A mensagem está vazia.'
          )
          error.status = 422
          throw error
        }

        const [checked] =
          await handle.sock.onWhatsApp(to)

        if (!checked?.exists) {
          const error = new Error(
            'O número do destinatário não está cadastrado no WhatsApp.'
          )
          error.status = 422
          throw error
        }

        let content
        const urls = extractHttpUrls(text)

        if (media) {
          const mediaType = String(media.type || '')
          const mime = String(media.mime || '')
          const filename = String(media.filename || 'arquivo').slice(0, 220)
          const rawData = String(media.data || '')
          const mediaCaption = urls.length ? captionWithoutUrls(text, urls) : text

          const allowedTypes = new Set([
            'image',
            'video',
            'document',
          ])

          const allowedMime = new Set([
            'image/jpeg',
            'image/png',
            'image/webp',
            'video/mp4',
            'application/pdf',
          ])

          if (!allowedTypes.has(mediaType) || !allowedMime.has(mime)) {
            const error = new Error(
              'Formato de mídia não suportado.'
            )
            error.status = 422
            throw error
          }

          let mediaBuffer

          try {
            mediaBuffer = Buffer.from(rawData, 'base64')
          } catch {
            const error = new Error(
              'Mídia inválida.'
            )
            error.status = 422
            throw error
          }

          if (!mediaBuffer.length || mediaBuffer.length > 2500000) {
            const error = new Error(
              'A mídia precisa ter até 2,5 MB.'
            )
            error.status = 422
            throw error
          }

          if (mediaType === 'image') {
            content = {
              image: mediaBuffer,
              mimetype: mime,
              caption: mediaCaption || undefined,
            }
          } else if (mediaType === 'video') {
            content = {
              video: mediaBuffer,
              mimetype: mime,
              caption: mediaCaption || undefined,
            }
          } else {
            content = {
              document: mediaBuffer,
              mimetype: mime,
              fileName: filename || 'documento.pdf',
              caption: mediaCaption || undefined,
            }
          }
        } else {
          const firstUrl = urls[0]
          content = firstUrl
            ? { text, linkPreview: linkPreviewFor(firstUrl) }
            : { text }
        }

        const sent =
          await handle.sock.sendMessage(
            checked.jid,
            content,
          )

        let messageId =
          sent?.key?.id

        // O WhatsApp não anexa metadados de link a captions de mídia do mesmo
        // jeito que faz em mensagens de texto. Quando houver URL junto de uma
        // imagem/vídeo/documento, movemos a URL para um segundo balão de texto
        // com matched-text/canonical-url explícitos. Assim o convite/link fica
        // clicável em Web, Desktop, Android e iOS.
        if (media && urls.length) {
          const linkText = urls.join('\n')
          const linkSent = await handle.sock.sendMessage(
            checked.jid,
            {
              text: linkText,
              linkPreview: linkPreviewFor(urls[0]),
            },
          )

          messageId = linkSent?.key?.id || messageId
        }

        if (!messageId) {
          throw new Error(
            'O WhatsApp não retornou o ID da mensagem.'
          )
        }

        // Uma espera curta basta para drenar eventos imediatos; o flush
        // abaixo persiste as credenciais antes de fechar o socket.
        await new Promise(
          resolve => setTimeout(resolve, 180)
        )

        await handle.flush()

        await pool.query(
          'INSERT INTO baileys_sent_requests (session_id, request_id, message_id) VALUES ($1,$2,$3) ON CONFLICT (session_id, request_id) DO NOTHING',
          [
            sessionId,
            body.requestId,
            messageId,
          ],
        )

        await upsertSession(
          sessionId,
          {
            status: 'connected',
            qrcode: null,
            lastError: null,
          },
        )

        console.info(
          '[Baileys] message sent',
          {
            sessionId,
            to,
            messageId,
          },
        )

        return {
          id: messageId,
        }
      } finally {
        try {
          await handle.flush()
        } catch {}

        handle.close()
      }
    })
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
    const result = await handleAction(action, sessionId, body, request)
    return response.status(action === 'create' ? 201 : 200).json(result)
  } catch (error) {
    console.error('[baileys]', { action, sessionId, error: String(error) })
    return response.status(error.status || 502).json({ error: error.message || 'Falha no Baileys.' })
  }
}
