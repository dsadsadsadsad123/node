# Deploy do mesmo V4 em múltiplos Route Nodes

Todos os serviços usam o mesmo repositório/código. Em cada servidor altere apenas:

- `VAIGO_NODE_ID` (`01`, `02`, ...)
- `VAIGO_NODE_NAME`
- `VAIGO_NODE_REGION`

Compartilhe entre os nodes:

- `CENTRAL_API_SECRET` — exatamente o mesmo do Central.
- `MAPBOX_ACCESS_TOKEN`.
- `DATABASE_URL` — opcional; o Central já envia contexto inline quando configurado.
- `REDIS_URL` — opcional; útil para cache entre workers/serviços que compartilham Redis.

Para Render free/pequeno use inicialmente `VAIGO_NODE_CAPACITY=4`. Se o p95 subir ou houver muitos timeouts, reduza para 3 antes de aumentar budgets de micro-rota.

No Admin do Central, mantenha:

- Health path: `/healthz`
- Route path: `/v1/route/calculate`
- Precalc path: `/v1/route/precalculate`

O Central já faz failover em 429, timeout e 5xx.
