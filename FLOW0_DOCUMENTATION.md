# Lago ↔ Paddle Integration — Technical Documentation

## Overview

This service acts as a bridge between Lago (billing) and Paddle (payment processor).
When a customer is created or updated in Lago, this integration automatically mirrors
that customer in Paddle and writes the Paddle IDs back into Lago for future use.

Built in Python using FastAPI + Celery + Redis.

---

## Architecture

```
Lago
  │  customer.created webhook (HTTP POST)
  ▼
FastAPI API  ──────────────────────────────  port 3000
  │  verify HMAC signature
  │  enqueue Celery task (task_id = customer-sync-{lago_id})
  ▼
Redis  ────────────────────────────────────  broker + result backend
  │  task queue
  ▼
Celery Worker
  │  idempotency check (Redis NX key)
  │
  ├──► Paddle API  →  find or create Customer (ctm_)
  ├──► Paddle API  →  find or create Address  (add_)
  ├──► Paddle API  →  create Business if B2B  (biz_)
  │
  └──► Lago API  →  store Paddle IDs in customer metadata
         │  set idempotency key in Redis (only on success)
```

### Infrastructure (Docker Compose)

| Service | Image | Port | Role |
|---------|-------|------|------|
| api | custom (FastAPI) | 3000 | Webhook receiver |
| worker | custom (Celery) | — | Async job processor |
| flower | custom (Celery Flower) | 5555 | Worker monitoring UI |
| redis | redis:7-alpine | 6380→6379 | Message broker + idempotency store |

The `api` and `worker` containers are joined to `lago_dev_default` (external network) so they can reach Lago's internal containers directly.

---

## Flow 0 — Customer Sync (Lago → Paddle)

### Trigger

A `customer.created` webhook fired by Lago whenever a new customer is saved.

---

### Step 1 — Webhook received

**File:** `app/webhooks/lago.py` → `lago_webhook()`

- FastAPI receives `POST /webhooks/lago`
- Runs `verify_lago_signature()` as a dependency before anything else
- Parses the JSON body, reads `webhook_type`
- If `customer.created`: enqueues a Celery task and returns `200 OK` immediately
- The API never waits for Paddle/Lago calls — the queue decouples reception from processing

---

### Step 2 — Signature verification

**File:** `app/webhooks/verify/lago.py` → `verify_lago_signature()`

- Reads `x-lago-signature` header from the request
- Computes `HMAC-SHA256(lago_webhook_secret, raw_body)` → base64-encodes it
- Compares using `hmac.compare_digest()` (constant-time, prevents timing attacks)
- Returns 401 if missing or invalid

---

### Step 3 — Task enqueued in Redis

**File:** `app/tasks/customer_sync.py` → `sync_customer()`

```python
sync_customer.apply_async(
    args=[customer_data],
    task_id=f"customer-sync-{lago_id}"
)
```

The `task_id` is deterministic — if the same webhook fires twice (Lago retries), Celery sees the ID already exists and silently drops the duplicate. No double-processing.

Task configuration:

| Setting | Value | Meaning |
|---------|-------|---------|
| `max_retries` | 5 | Retry up to 5 times on any exception |
| `retry_backoff` | True | Exponential backoff: 1s, 2s, 4s… |
| `retry_backoff_max` | 60s | Cap on retry delay |
| `acks_late` | True | Task stays in queue until it succeeds; worker crash = auto re-queue |

---

### Step 4 — Idempotency check

**File:** `app/utils/idempotency.py` → `set_if_not_exists()`

Before doing any work, the task checks:

```python
if _redis.get(f"customer-sync:{customer.lago_id}"):
    return  # already processed successfully, skip
```

This key is only written **after** the full flow succeeds. If the task fails halfway and retries, it will re-run everything cleanly.

---

### Step 5 — Ensure Paddle Customer

**File:** `app/flows/customer_sync.py` → `_ensure_paddle_customer()`
**File:** `app/clients/paddle.py` → `find_customer_by_email()`, `create_customer()`

1. `GET /customers?search={email}&status=active` — look for existing customer
2. If found → reuse it (return `id`)
3. If not found → `POST /customers {email, name}`
4. If Paddle returns 409 (email conflict with archived customer):
   - Search active customers → reuse if found
   - Search archived customers → if found, `PATCH /customers/{id} {status: active}` to unarchive
5. Returns `ctm_xxx`

---

### Step 6 — Ensure Paddle Address

**File:** `app/flows/customer_sync.py` → `_ensure_paddle_address()`
**File:** `app/clients/paddle.py` → `get_addresses()`, `create_address()`

1. `GET /customers/{id}/addresses` — check for existing addresses
2. If any exist → reuse the first one (return `id`)
3. If none → `POST /customers/{id}/addresses` with address fields
4. Skipped entirely if the customer has no `country` — Paddle requires it for tax calculation
5. Returns `add_xxx` or `None`

---

### Step 7 — Create Paddle Business (B2B only)

**File:** `app/flows/customer_sync.py` → `_create_paddle_business_if_needed()`
**File:** `app/clients/paddle.py` → `create_business()`

1. Skip if `legal_name` or `tax_identification_number` is missing (B2C customer)
2. `POST /customers/{id}/businesses {name, tax_identifier}`
3. Returns `biz_xxx` or `None`

---

### Step 8 — Write Paddle IDs back to Lago

**File:** `app/clients/lago.py` → `store_paddle_ids()`

```json
POST /api/v1/customers
{
  "customer": {
    "external_id": "...",
    "metadata": [
      {"key": "paddle_customer_id", "value": "ctm_xxx", "display_in_invoice": false},
      {"key": "paddle_address_id",  "value": "add_xxx", "display_in_invoice": false},
      {"key": "paddle_business_id", "value": "biz_xxx", "display_in_invoice": false}
    ]
  }
}
```

Uses Lago's upsert endpoint (`POST /customers`). `integration_customers` is not used because Lago OSS does not support custom third-party integration types — sending it triggers a `NotImplementedError` in Lago's Rails code.

> **Note:** The request includes `Host: api.lago.dev` header (configured via `LAGO_API_HOST` env var). Required in local dev because Lago runs behind Traefik which routes by hostname. Omit in production if `LAGO_API_URL` points directly to Lago.

After success: writes `customer-sync:{lago_id}` to Redis with a 24h TTL to prevent any future re-processing.

---

## Field Mapping

| Lago Field | Paddle Field | Paddle Object | Notes |
|------------|-------------|---------------|-------|
| `email` | `email` | Customer (`ctm_`) | Pivot key — used to find existing customer |
| `name` | `name` | Customer | Falls back to email if not set |
| `country` | `country_code` | Address (`add_`) | ISO 3166-1 alpha-2. Required for tax |
| `zipcode` | `postal_code` | Address | |
| `state` | `region` | Address | |
| `city` | `city` | Address | |
| `address_line1` | `first_line` | Address | |
| `address_line2` | `second_line` | Address | |
| `legal_name` | `name` | Business (`biz_`) | B2B only |
| `tax_identification_number` | `tax_identifier` | Business | EU VAT — B2B only |

---

## Configuration

All configuration is read from `.env` and parsed by `app/config.py` (pydantic-settings). The app crashes at startup if any required variable is missing.

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `LAGO_API_URL` | No | `https://api.getlago.com/api/v1` | Lago API base URL |
| `LAGO_API_HOST` | No | `None` | Override HTTP Host header (local dev with Traefik) |
| `LAGO_API_KEY` | **Yes** | — | Lago API key |
| `LAGO_WEBHOOK_SECRET` | **Yes** | — | HMAC secret for webhook signature verification |
| `PADDLE_API_URL` | No | `https://api.paddle.com` | Paddle API base URL (use `sandbox-api.paddle.com` for dev) |
| `PADDLE_API_KEY` | **Yes** | — | Paddle API key |
| `REDIS_URL` | No | `redis://localhost:6379/0` | Redis connection string |
| `PORT` | No | `3000` | API server port |

---

## File Structure

```
app/
├── main.py                    # FastAPI app, router registration
├── config.py                  # Env var parsing (pydantic-settings)
├── worker.py                  # Celery app definition
│
├── webhooks/
│   ├── lago.py                # POST /webhooks/lago endpoint
│   └── verify/
│       └── lago.py            # HMAC signature verification
│
├── tasks/
│   └── customer_sync.py       # Celery task: idempotency + retry config
│
├── flows/
│   └── customer_sync.py       # Business logic orchestration
│
├── clients/
│   ├── paddle.py              # Paddle API calls (httpx)
│   └── lago.py                # Lago API calls (httpx)
│
├── models/
│   └── lago.py                # LagoCustomer pydantic model
│
└── utils/
    ├── idempotency.py         # Redis NX key helper
    └── logger.py              # structlog configuration
```

---

## Key Design Decisions

### Why a queue instead of calling Paddle directly in the webhook handler?

Lago expects a `200 OK` within a few seconds. Paddle/Lago API calls can be slow or fail. The queue decouples reception from processing — Lago gets its 200 immediately, and if Paddle is down the worker retries automatically with no data loss.

### Why idempotency at two levels?

- **task_id dedup** (Celery/Redis): prevents the same event from being enqueued twice if Lago retries the webhook
- **NX key after success** (Redis): prevents re-processing if the worker restarts mid-task between retries

### Why `metadata` instead of `integration_customers`?

Lago OSS does not implement custom integration types. Sending `integration_customers` with `type: "paddle"` triggers a `NotImplementedError` in Lago's Rails code (`base_integration.rb`). The `metadata` field is the correct OSS-compatible storage for custom third-party IDs.

### Why `Host: api.lago.dev`?

In local dev, Lago runs behind Traefik which routes requests by `Host` header. The URL `http://host.docker.internal/api/v1` reaches Traefik's port 80, but without `Host: api.lago.dev` Traefik returns 404. In production this override is not needed if `LAGO_API_URL` points directly to Lago's host.
