# VAIGO Route Node V4

Worker stateless/leve do VAIGO para processamento distribuído de rotas. O V4 mantém o contrato do Central V90+ (`/healthz`, `/v1/route/calculate` e `/v1/route/precalculate`) e concentra as otimizações no hot path de cálculo.

## O que mudou no V4

- **Fastest realmente ETA-only:** zonas de segurança e perfil profissional não contaminam o modo `fastest`; `safest`/`smart` continuam aplicando contexto e bloqueios.
- **Uma onda de variantes:** micro-rotas, variantes adaptativas e de segurança são disparadas por um pool compartilhado e limitado, sem criar vários pools de threads por usuário.
- **Geometria preparada:** checagens de distância de relatos, zonas e fluxo reutilizam uma geometria projetada da rota em vez de recalcular trigonometria para cada ponto.
- **Cache mais barato:** fallback em memória usa cópia direta e LRU; `fastest` não fragmenta cache por contexto de segurança.
- **Coalescing de requests:** cálculos idênticos simultâneos compartilham o resultado e não ocupam outro slot pesado.
- **HTTP Mapbox reaproveitado:** pool persistente de conexões, limite global de concorrência e timeout alinhado ao failover do Central.
- **Fail-fast:** se a primeira chamada Mapbox já veio lenta, o node evita gastar outra onda pesada de variantes e devolve a melhor base a tempo.
- **JSON gzip:** respostas grandes de GeoJSON são comprimidas quando o Central anuncia suporte a gzip; `requests` descomprime automaticamente.
- **Telemetria:** `/healthz` continua compatível e agora inclui contadores de provider/coalescing.

## Endpoints compatíveis

- `GET /livez` — liveness do processo; usado pelo Render.
- `GET /healthz` — telemetria do node; HTTP 200 mesmo se Mapbox estiver ausente, com `healthy=false`.
- `GET /readyz` — readiness real do motor Mapbox.
- `GET /v1/metrics` — métricas autenticadas.
- `GET /v1/config` — configuração pública autenticada.
- `POST /v1/route/calculate` — calcula um modo.
- `POST /v1/route/precalculate` — calcula/cacheia vários modos a partir do mesmo conjunto de candidatos.
- `POST /v1/cache/invalidate` — invalidação pontual autenticada.

## Segurança

Todo `/v1/*` exige o mesmo `CENTRAL_API_SECRET` configurado no servidor Central. Sem segredo, apenas smoke test local pode ser habilitado explicitamente com `VAIGO_ALLOW_INSECURE_LOCAL=1`.

## Capacidade recomendada

Em instância free/pequena, comece com:

- `VAIGO_NODE_CAPACITY=4`
- `VAIGO_GUNICORN_THREADS=8`
- `VAIGO_PROVIDER_MAX_CONCURRENCY=8`
- `VAIGO_VARIANT_WORKERS=6`

A capacidade é de **jobs pesados**, não de requests HTTP. Health checks e cache hits continuam leves. Ao lotar, o node retorna HTTP `429 node_capacity_reached` para o Central trocar de worker.

## Render

Build:

`python -m pip install --upgrade pip && python -m pip install -r requirements.txt && python validate.py && python -c "import app; print('VAIGO NODE V4 IMPORT OK')"`

Start:

`python -m gunicorn app:app -c gunicorn.conf.py`

Health Check Path:

`/livez`
