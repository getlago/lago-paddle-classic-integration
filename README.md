# Lago × Paddle Classic Integration

A self-hosted middleware that bridges [Lago](https://getlago.com) (usage-based billing) and [Paddle Classic](https://classic.paddle.com) (payment processor).

Built with FastAPI + Celery + Redis.

---

## How it works

Lago handles metering, invoicing, and wallets. Paddle Classic handles payments and subscriptions. This middleware listens to webhooks from both sides and keeps them in sync.

### Flows

**Flow 1 — Lago-first onboarding** (`customer.created` → Paddle checkout)

When a customer is created in Lago, the middleware stores a `/checkout/{external_id}` link in Lago metadata. The customer visits that link, selects a plan (or is redirected directly for single-plan setups), completes Paddle checkout, and `subscription_created` fires — which activates billing.

```
Lago customer.created
  → store /checkout/{external_id} URL in Lago metadata
  → customer visits checkout link
  → selects plan (picker shown if multiple plans available)
  → completes Paddle checkout
  → Paddle subscription_created
  → store paddle_sub_{plan_id} in Lago metadata
  → create Lago subscription + wallet (if plan has create_wallet: true)
```

**Flow 2 — Paddle-first onboarding** (`subscription_created` → Lago customer)

When a subscription is created directly in Paddle (no prior Lago customer), the middleware creates the customer in Lago automatically, then activates billing.

```
Paddle subscription_created (empty passthrough)
  → create customer in Lago from Paddle data
  → store paddle_sub_{plan_id} in Lago metadata
  → create Lago subscription + wallet (if plan has create_wallet: true)
```

**Flow 3 — Wallet top-up**

Two directions depending on who initiates the charge:

*3a — External Paddle charge* (`subscription_payment_succeeded`): a charge is triggered directly against a Paddle subscription (e.g. from your app or a customer portal). The middleware catches the payment webhook, tops up the Lago wallet, and marks the resulting credit invoice as paid — no second Paddle charge is made.

```
External Paddle charge (POST /subscription/{id}/charge)
  → Paddle subscription_payment_succeeded
  → read lago_external_id from passthrough (fallback: user_id)
  → top up Lago wallet with sale_gross amount
  → Lago invoice.generated (type: credit)
  → mark credit invoice as paid → credits become available
```

*3b — Lago-initiated top-up* (`invoice.generated` type: credit): a wallet transaction is created directly in Lago (e.g. from the Lago UI or API). The middleware charges the customer's Paddle subscription for the invoice amount, then marks it paid.

```
Lago invoice.generated (type: credit)
  → no external_topup flag in Redis → Lago-initiated
  → look up paddle_sub_{plan_id} from customer metadata
  → charge Paddle subscription
  → mark Lago invoice as paid → credits become available
```

The middleware distinguishes the two using a short-lived Redis flag (`external_topup:{customer_id}`) set when an external charge is processed.

Flow 3a is skipped for:
- `$0` renewals (`sale_gross = 0`)
- Customers with no active Lago wallet
- Charges triggered by the middleware for overage invoices (Flow 4)

**Flow 4 — Overage billing** (`invoice.generated` type: subscription)

When Lago generates a subscription invoice (customer exceeded their usage quota), the middleware charges the Paddle subscription on file and settles the invoice.

```
Lago invoice.generated (type: subscription)
  → look up paddle_sub_{plan_id} from customer metadata
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
cp .env.example .env
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

Open `http://localhost:3000` in your browser and fill in the setup form.

**Lago**

| Field | Value |
|---|---|
| **Lago API URL** | `https://api.getlago.com/api/v1` (Cloud) or your self-hosted URL |
| **Lago API Key** | Lago → Settings → API keys |
| **Webhook Secret** | Lago → Settings → Developers → Webhooks → HMAC signature |

**Paddle Classic**

| Field | Value |
|---|---|
| **Paddle API URL** | `https://vendors.paddle.com/api/2.0` (production) or sandbox URL |
| **Paddle Vendor ID** | Your Paddle vendor ID |
| **Paddle Auth Code** | Your Paddle vendor auth code |
| **Paddle Public Key** *(optional)* | Paddle Dashboard → Developer Tools → Public Key — used to verify webhook signatures |

**Plans**

At least one plan is required. Each row maps a Paddle plan to a Lago plan:

| Field | Value |
|---|---|
| **Paddle Plan ID** | ID of a Paddle subscription plan (Catalog → Subscription Plans) |
| **Lago Plan Code** | An existing Lago plan code — leave blank on single-plan setups to auto-create one |
| **Wallet** | Check to enable a prepaid credit wallet for this plan (uncheck for entitlement/flat-rate plans) |

Multiple plans are supported. Each plan gets its own subscription metadata key on the Lago customer — e.g. `paddle_sub_89290` for Paddle plan ID `89290`. This allows customers to hold subscriptions to multiple plans simultaneously, and ensures overage invoices are charged to the correct Paddle subscription.

**App**

| Field | Value |
|---|---|
| **Middleware URL** | Your public URL — e.g. your ngrok URL |

Clicking **Connect** will:
- Validate both Lago and Paddle credentials
- Register the webhook endpoint in Lago (and clean up any stale ones)
- Auto-create an `ai_tokens` billable metric and `ai_tokens_plan` in Lago for single-plan setups with no plan code provided — skipped if you supply your own plan code
- Save all config to Redis + a durable file (survives Redis restarts)

#### Bringing your own Lago plan

If you already have a Lago plan configured, paste its code into the **Lago Plan Code** field. The middleware will use it as-is — no billable metric or plan will be created. Your plan must include a charge that tracks token usage (any `sum_agg` metric works).

> **Note on pricing:** Lago tracks usage and computes invoice amounts based on the charge prices defined in your plan. Paddle charges are driven by those amounts — no pricing is set in the middleware itself.

> **Paddle webhook — manual step:** setup does not register the webhook in Paddle Classic. Go to Paddle Dashboard → Developer Tools → Events → URLs for receiving webhooks → Add endpoint, and point it to `{your-middleware-url}/webhooks/paddle`. Make sure to enable at least `subscription_created`, `subscription_payment_succeeded`, and `subscription_cancelled` alert types.

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

The API returns `200 OK` to webhooks immediately and offloads all work to the Celery worker. This means neither Lago nor Paddle ever times out waiting for the other side's API calls.

### Idempotency

- **Webhook deduplication**: Celery tasks use deterministic task IDs (`onboarding-{lago_id}`, `invoice-payment-{invoice_id}`). Duplicate webhooks are silently dropped.
- **Paddle charge deduplication**: Before charging Paddle, the worker checks Redis for an existing `order_id` for that invoice. If found, the charge is skipped and the invoice is marked paid directly.
- **Wallet top-up deduplication**: Each `subscription_payment_id` is recorded in Redis after processing. Duplicate `subscription_payment_succeeded` webhooks are ignored.
- **Middleware charge tagging**: Paddle charges triggered by the middleware are tagged with `middleware_order:{order_id}` in Redis so the `subscription_payment_succeeded` handler skips them and doesn't double-credit the wallet.
- **External top-up flag**: when an external charge tops up a wallet, a short-lived `external_topup:{customer_id}` flag is set in Redis. The credit invoice handler reads this flag to decide whether to mark the invoice paid directly (external) or charge Paddle first (Lago-initiated).
- **Lago API idempotency**: Lago subscriptions use a stable `external_id` (`paddle-sub-{subscription_id}`). Wallets handle `422 already_exists` gracefully.

---

## Monitoring

A live status dashboard is available at `http://localhost:3000/status` showing:
- Middleware configuration status
- Lago API connectivity
- Paddle API connectivity
- Live log stream (last 500 entries)
