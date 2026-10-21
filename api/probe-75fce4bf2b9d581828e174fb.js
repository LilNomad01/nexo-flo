import pg from 'pg'

export const maxDuration = 60

export default async function handler(req, res) {
  if (req.method !== 'GET') {
    return res.status(405).json({ ok: false, error: 'method_not_allowed' })
  }

  const oldUrl = process.env.OLD_DATABASE_URL
  if (!oldUrl) {
    return res.status(500).json({ ok: false, error: 'missing_old_database_url' })
  }

  const pool = new pg.Pool({
    connectionString: oldUrl,
    max: 1,
    idleTimeoutMillis: 5000,
    connectionTimeoutMillis: 8000,
    ssl: { rejectUnauthorized: false },
  })

  try {
    const client = await pool.connect()
    try {
      const info = await client.query('select current_database() as database_name, current_user as database_user, now() as checked_at')
      const tables = await client.query(
        "select relname as table_name, n_live_tup::bigint as approx_rows from pg_stat_user_tables order by relname"
      )
      return res.status(200).json({ ok: true, info: info.rows[0], tables: tables.rows })
    } finally {
      client.release()
    }
  } catch (error) {
    return res.status(503).json({
      ok: false,
      error: 'old_database_unavailable',
      code: error?.code || null,
      message: String(error?.message || error).slice(0, 1000),
    })
  } finally {
    await pool.end().catch(() => {})
  }
}
