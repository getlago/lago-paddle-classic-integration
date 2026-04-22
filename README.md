# Lago × Paddle Classic Integration

A self-hosted middleware that bridges [Lago](https://getlago.com) (usage-based billing) and [Paddle Classic](https://classic.paddle.com) (payment processor).

Built with FastAPI + Celery + Redis.

---

## How it works

Lago handles metering, invoicing, and wallets. Paddle Classic handles payments and subscriptions. This middleware listens to webhooks from both sides and keeps them in sync.

### Flows

**Flow 1 — Lago-first onboarding** (`customer.created` → Paddle checkout)

When a customer is created in Lago, the middleware generates a Paddle checkout link and caches it. The customer clicks the link, completes checkout, and Paddle fires `subscription_created` — which stores the subscription ID in Lago metadata and activates billing.

```
Lago customer.created
  → generate Paddle checkout link
  → cache URL in Redis
  → customer completes checkout
  → Paddle subscription_created
  → store paddle_sub_id in Lago metadata
  → create Lago subscription + wallet
```

**Flow 2 — Paddle-first onboarding** (`subscription_created` → Lago customer)

When a subscription is created directly in Paddle (no prior Lago customer), the middleware creates the customer in Lago automatically, then stores the subscription ID and activates billing.

```
Paddle subscription_created (empty passthrough)
  → create customer in Lago from Paddle data
  → store paddle_sub_id in Lago metadata
  → create Lago subscription + wallet
```

**Flow 3 — Wallet top-up** (`invoice.generated` type: credit)

When Lago generates a credit invoice (customer bought tokens), the middleware charges the Paddle subscription on file and marks the invoice as paid.

```
Lago invoice.generated (type: credit)
  → charge Paddle subscription
  → mark Lago invoice as paid
```

**Flow 4 — Overage** (`invoice.generated` type: subscription)

When Lago generates an overage invoice (customer exceeded their token quota), same as above — charge Paddle and settle the invoice.

```
Lago invoice.generated (type: subscription)
  → charge Paddle subscription
  → mark Lago invoice as paid
```

---

## Setup

### Prerequisites

- Docker + Docker Compose
- A [Lago](https://getlago.com) account (Cloud or self-hosted)
- A [Paddle Classic](https://classic.paddle.com) account
- A publicly reachable URL for webhook delivery (see ngrok section below)

### 1. Clone and start

```bash
git clone https://github.com/getlago/lago-paddle-classic-integration.git
cd lago-paddle-classic-integration
cp .env.example .env   # edit with your Redis URL if needed
docker compose up -d
```

### 2. Expose your middleware publicly (local dev)

Lago needs to deliver webhooks to your middleware. In local development, your machine is not publicly reachable — use [ngrok](https://ngrok.com) to create a tunnel:

```bash
ngrok http 3000
```

Copy the `https://xxxx.ngrok-free.app` URL — you'll need it in the next step.

> **Note:** On the free ngrok plan the URL changes every time you restart ngrok. Re-run setup whenever this happens to update the webhook registration in Lago.

### 3. Run setup

Open `http://localhost:3000` in your browser and fill in the setup form:

| Field | Value |
|---|---|
| **Lago API URL** | `https://api.getlago.com/api/v1` (Cloud) or your self-hosted URL |
| **Lago API Key** | Lago → Settings → API keys |
| **Webhook Secret** | Lago → Settings → Developers → Webhooks → HMAC signature |
| **Paddle API URL** | `https://vendors.paddle.com/api/2.0` (production) or sandbox |
| **Paddle Vendor ID** | Your Paddle vendor ID |
| **Paddle Auth Code** | Your Paddle vendor auth code |
| **Subscription Plan ID** | A `$0/mo` monthly plan created in Paddle → Catalog → Subscription Plans |
| **Webhook URL** | Your ngrok URL (e.g. `https://xxxx.ngrok-free.app`) |

Clicking **Connect** will:
- Validate both Lago and Paddle credentials
- Register the webhook endpoint in Lago (and clean up any stale ones)
- Create the `ai_tokens` billable metric and `ai_tokens_plan` in Lago
- Save all config to Redis + a durable file (survives Redis restarts)

---

## Architecture

```
                    ┌─────────────────────────────┐
                    │         FastAPI API          │  :3000
                    │  webhook receiver + setup UI │
                    └────────────┬────────────────-┘
                                 │ enqueue
                    ┌────────────▼────────────────┐
                    │           Redis              │  :6380
                    │   broker + config store      │
                    └────────────┬────────────────-┘
                                 │ consume
                    ┌────────────▼────────────────┐
                    │       Celery Worker          │
                    │   async job processor        │
                    └─────────────────────────────┘
```

The API returns `200 OK` to Lago immediately and offloads all work to the Celery worker. This means Lago never times out waiting for Paddle API calls.

### Idempotency

- **Webhook deduplication**: Celery tasks are enqueued with a deterministic `task_id` (`onboarding-{lago_id}`, `invoice-payment-{invoice_id}`). Duplicate webhooks from Lago are silently dropped.
- **Paddle charge deduplication**: Before charging Paddle, the worker checks Redis for a stored `order_id` for that invoice. If found, the charge is skipped and the invoice is marked paid directly. This ensures Paddle is never charged twice even if the task retries.
- **Lago API idempotency**: Lago subscriptions use a stable `external_id` (`paddle-sub-{subscription_id}`). Wallets handle `422 already_exists` gracefully.

### Config persistence

Config is stored in two layers:
1. **Redis hash** — fast path, shared between API and worker
2. **`/data/config.json`** on a named Docker volume — survives Redis restarts

On API startup, if the Redis hash is missing it is rehydrated from the file automatically.

---

## Monitoring

A live status dashboard is available at `http://localhost:3000/status` showing:
- Middleware configuration status
- Lago API connectivity
- Paddle API connectivity
- Live log stream (last 500 entries)
