# Integração Central ↔ VAIGO Route Node V4

O V4 é drop-in para o Central atual. Não é necessário alterar `route_path`, `precalc_path` ou o formato do payload.

O Central envia:

`Authorization: Bearer <CENTRAL_API_SECRET>`

## Cálculo único

`POST /v1/route/calculate`

```json
{
  "request_id": "abc123",
  "start": {"lat": -23.60, "lon": -46.72},
  "end": {"lat": -23.56, "lon": -46.66},
  "profile": "driving",
  "mode": "fastest",
  "depart_at": "now",
  "heading": 92,
  "speed": 8.5,
  "reroute": false,
  "adaptive": true,
  "preferences": {
    "avoid_ferries": false,
    "avoid_tolls": false,
    "avoid_unpaved": false
  },
  "professional_driver": false,
  "local_hour": 12,
  "night_active": false,
  "safety_bias": 68,
  "traffic_bias": 62
}
```

O Central atual também pode anexar `context` com `reports`, `risk_zones` e `flow_samples`. O V4 usa esse contexto em `safest/smart/quietest`; `fastest` permanece ETA-only, conforme o comportamento do Central local.

## Pré-cálculo

`POST /v1/route/precalculate`

```json
{
  "mode": "safest",
  "modes": ["safest", "fastest", "smart"],
  "start": {"lat": -23.60, "lon": -46.72},
  "end": {"lat": -23.56, "lon": -46.66}
}
```

A resposta continua usando `results.<mode>.routes`, exatamente como o dispatcher atual do Central espera.

## Capacidade/failover

Quando não há slot:

```json
{
  "ok": false,
  "error": "node_capacity_reached",
  "retryable": true,
  "retry_after_ms": 500
}
```

HTTP 429 também inclui `Retry-After: 1`. O Central atual ignora/aceita esse campo extra e tenta o próximo servidor normalmente.

## Health

`GET /healthz` continua retornando HTTP 200 para processo vivo. Se Mapbox não estiver configurado, o corpo inclui `healthy=false` e `mapbox_ready=false`, compatível com o painel Admin atual.
