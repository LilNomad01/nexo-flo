import { randomUUID } from 'node:crypto'
import pg from 'pg'

export const maxDuration = 60

function id(prefix) {
  return prefix + '_' + randomUUID().replaceAll('-', '')
}

export default async function handler(req, res) {
  if (req.method !== 'GET') return res.status(404).json({ ok: false })

  const token = String(req.query?.token || '')
  if (!process.env.BOOTSTRAP_TOKEN || token !== process.env.BOOTSTRAP_TOKEN) {
    return res.status(404).json({ ok: false })
  }

  const email = String(process.env.BOOTSTRAP_EMAIL || '').trim().toLowerCase()
  const name = String(process.env.BOOTSTRAP_NAME || '').trim()
  const workspace = String(process.env.BOOTSTRAP_WORKSPACE || '').trim()
  const passwordHash = String(process.env.BOOTSTRAP_PASSWORD_HASH || '')
  const databaseUrl = process.env.DATABASE_URL

  if (!databaseUrl || !email || !name || !workspace || !passwordHash) {
    return res.status(500).json({ ok: false, error: 'missing_bootstrap_config' })
  }

  const pool = new pg.Pool({
    connectionString: databaseUrl,
    max: 1,
    connectionTimeoutMillis: 10000,
    ssl: { rejectUnauthorized: false },
  })

  const client = await pool.connect()
  try {
    await client.query('BEGIN')
    const existing = await client.query(
      'select u.id, u.email, wm.workspace_id from users u left join workspace_members wm on wm.user_id=u.id where lower(u.email)=lower($1) limit 1',
      [email],
    )

    if (existing.rows[0]) {
      await client.query('COMMIT')
      return res.status(200).json({ ok: true, created: false, email, workspace_id: existing.rows[0].workspace_id || null })
    }

    const userId = id('usr')
    const workspaceId = id('wsp')
    const memberId = id('mem')
    const logId = id('log')

    await client.query(
      'insert into users (id,email,name,password_hash,is_active,created_at,updated_at) values ($1,$2,$3,$4,true,now(),now())',
      [userId, email, name, passwordHash],
    )
    await client.query(
      'insert into workspaces (id,name,created_at,updated_at) values ($1,$2,now(),now())',
      [workspaceId, workspace],
    )
    await client.query(
      'insert into workspace_members (id,workspace_id,user_id,role,created_at,updated_at) values ($1,$2,$3,$4,now(),now())',
      [memberId, workspaceId, userId, 'owner'],
    )
    await client.query(
      'insert into system_logs (id,workspace_id,level,category,event,message,details_json,created_at) values ($1,$2,$3,$4,$5,$6,$7,now())',
      [logId, workspaceId, 'success', 'system', 'workspace.created', 'Workspace criado.', '{}'],
    )

    await client.query('COMMIT')
    return res.status(200).json({ ok: true, created: true, email, workspace_id: workspaceId })
  } catch (error) {
    await client.query('ROLLBACK').catch(() => {})
    return res.status(500).json({ ok: false, error: String(error?.message || error).slice(0, 1000) })
  } finally {
    client.release()
    await pool.end().catch(() => {})
  }
}
