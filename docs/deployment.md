# Deployment

## Configuration

Everything is set through environment variables. Compose reads them from `.env`.

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `AR_JWT_SECRET` | yes | | Signs dashboard sessions. At least 32 characters. |
| `POSTGRES_PASSWORD` | yes | | Password for the bundled Postgres |
| `AR_BOOTSTRAP_ADMIN_EMAIL`, `AR_BOOTSTRAP_ADMIN_PASSWORD` | first start | | Creates the first admin when no users exist. Password at least 12 characters. |
| `AR_COOKIE_SECURE` | behind HTTPS | `false` | Set `true` so the session cookie is only sent over HTTPS |
| `AR_CORS_ORIGINS` | no | none | Comma-separated origins, only if the dashboard is on a different origin from the API |
| `AR_TRUSTED_PROXIES` | no | `172.16.0.0/12` | Networks whose `X-Forwarded-For` is believed (for sign-in throttling and audit) |
| `AR_API_WORKERS` | no | `2` | API worker processes |
| `AR_METRICS_TOKEN` | no | none | Require this bearer token on `GET /metrics` |
| `AR_EVENTS_PER_SECOND` | no | `100` | Cap on decision-log events per API process per second |
| `AR_DATABASE_URL`, `AR_REDIS_URL` | no | bundled services | Point at managed Postgres or Redis instead |

Generate secrets with `python -c "import secrets; print(secrets.token_urlsafe(48))"`.

## A single EC2 instance

Suitable for a team or a modest production load. Everything runs on one machine.

1. Launch an instance (for example `t3.medium`, Ubuntu 24.04) with a security group that allows
   inbound 443 (and 80 for certificate issuance) from where your users are, and 22 from your own
   address only. Do not open 8000, 6379 or 5432.
2. Install Docker and the Compose plugin, then:

   ```bash
   git clone https://github.com/Ari-Han-t/AdaptiRate.git && cd AdaptiRate
   cp .env.example .env    # fill in; set AR_COOKIE_SECURE=true
   docker compose up -d
   ```

3. Put TLS in front. The bundled dashboard container listens on `127.0.0.1:8080`; terminate HTTPS
   with Caddy, or Nginx plus certbot, on the host and proxy to it. With Caddy:

   ```
   limits.example.com {
       reverse_proxy 127.0.0.1:8080
   }
   ```

4. Services in the same VPC should reach the API privately. Publish it on the instance's private
   address by changing the `api` port mapping to `"<private-ip>:8000:8000"`, and allow 8000 only
   from your services' security group. Then set `ADAPTIRATE_URL=http://<private-ip>:8000` in
   those services.

### Backups

Postgres holds everything that cannot be rebuilt: policies, keys, users, audit log.

```bash
docker compose exec postgres pg_dump -U adaptirate adaptirate | gzip > adaptirate-$(date +%F).sql.gz
```

Redis holds only live counters and adaptive state. Losing it resets everyone's usage and returns
limits to their configured values; nothing permanent is lost.

## Scaling up

- **More decisions per second:** raise `AR_API_WORKERS`, or run several API containers behind a
  load balancer. They are stateless. Then consider batch or leased mode in the SDKs.
- **Availability of the adaptive loop:** run two worker containers. One leads; the other takes
  over within about five seconds if it stops.
- **Managed data stores:** set `AR_DATABASE_URL` and `AR_REDIS_URL` and remove the bundled
  services. Redis must be a single primary (not Cluster) and should use `noeviction`, since
  evicting limiter keys silently resets limits.

## Upgrading

```bash
git pull
docker compose up -d --build
```

The `migrate` service applies database migrations before the API starts.

## Monitoring AdaptiRate itself

- The dashboard's Monitoring page shows Redis, Postgres, worker and API process health.
- `GET /ready` returns 503 when Redis or Postgres is unreachable; use it for load balancer checks.
- `docker compose --profile observability up -d` adds Prometheus scraping `GET /metrics`
  (decision counters per policy, effective limit, multiplier, health score).

## If AdaptiRate is unavailable

The SDK decides what your service does, per its fail mode:

| Mode | Behaviour | Use when |
|---|---|---|
| `open` (default) | Allow every request | Availability matters more than the limit |
| `closed` | Deny every request | The limit protects something that must not be exceeded |
| `local` | Enforce the last limit the server reported, in process | You want protection to continue through an outage |

After a failed call the SDK stops contacting the server for one second, so an outage does not add
a timeout to every request.
