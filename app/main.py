import json
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from pathlib import Path

from app.webhooks.lago import router as lago_webhook_router
from app.webhooks.paddle import router as paddle_webhook_router
from app.api.setup import router as setup_router
from app.api.status_api import router as status_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Rehydrate Redis from the durable config file if the hash is missing
    from app.utils.config_store import rehydrate_redis
    rehydrate_redis()
    yield


app = FastAPI(title="Lago-Paddle Integration", lifespan=lifespan)

# Static files (setup UI)
static_path = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(static_path)), name="static")

# Routers
app.include_router(lago_webhook_router)
app.include_router(paddle_webhook_router)
app.include_router(setup_router)
app.include_router(status_router)


@app.get("/")
async def root():
    """Redirect root to setup UI."""
    return FileResponse(str(static_path / "setup.html"))


@app.get("/status")
async def status_page():
    return FileResponse(str(static_path / "status.html"))


@app.get("/health")
async def health():
    from app.utils.config_store import is_configured
    return {
        "status": "ok",
        "configured": is_configured(),
    }


def _render_plan_picker(external_id: str, plans: list) -> str:
    cards = ""
    for p in plans:
        lago_plan_code = p.get("lago_plan_code", "")
        paddle_plan_id = p.get("paddle_plan_id", "")
        cards += f"""
        <a class="plan" href="/checkout/{external_id}?plan={paddle_plan_id}">
          <div class="plan-name">{lago_plan_code}</div>
          <span class="plan-cta">Select →</span>
        </a>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>Choose a plan</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #f5f5f7;
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 2rem;
    }}
    .card {{
      background: white;
      border-radius: 12px;
      box-shadow: 0 2px 20px rgba(0,0,0,0.08);
      width: 100%;
      max-width: 520px;
      padding: 2.5rem;
    }}
    .logo {{
      display: flex;
      align-items: center;
      gap: 0.75rem;
      margin-bottom: 2rem;
    }}
    h1 {{ font-size: 1.125rem; font-weight: 600; color: #1a1a1a; margin-bottom: 0.5rem; }}
    p {{ font-size: 0.875rem; color: #6b7280; margin-bottom: 1.75rem; }}
    .plans {{ display: flex; flex-direction: column; gap: 0.75rem; }}
    .plan {{
      border: 1px solid #e5e7eb;
      border-radius: 10px;
      padding: 1.25rem 1.5rem;
      display: flex;
      align-items: center;
      justify-content: space-between;
      cursor: pointer;
      text-decoration: none;
      transition: border-color 0.15s, box-shadow 0.15s;
    }}
    .plan:hover {{
      border-color: #6366f1;
      box-shadow: 0 0 0 3px rgba(99,102,241,0.1);
    }}
    .plan-name {{ font-size: 0.9375rem; font-weight: 600; color: #111827; }}
.plan-cta {{
      background: #6366f1;
      color: white;
      border-radius: 7px;
      padding: 0.5rem 1.125rem;
      font-size: 0.8125rem;
      font-weight: 600;
      white-space: nowrap;
    }}
  </style>
</head>
<body>
  <div class="card">
    <div class="logo">
      <img src="https://getlago.com/logo.svg" alt="Lago" style="height:24px;width:auto;" />
      <span style="color:#9ca3af; font-size:0.875rem; margin-left:auto;">Checkout</span>
    </div>
    <h1>Choose a plan</h1>
    <p>Select the plan that fits your needs. You'll be taken to a secure Paddle checkout.</p>
    <div class="plans">
      {cards}
    </div>
  </div>
</body>
</html>"""


@app.get("/checkout/{external_id:path}")
async def checkout_page(external_id: str, plan: str = Query(default=None)):
    """
    Lago-first checkout entry point.

    - Single plan configured → generate Paddle pay link and redirect immediately.
    - Multiple plans configured → render a plan picker page.
    - ?plan=<paddle_plan_id> query param → generate pay link for that plan and redirect.
    """
    import redis as redis_lib
    from app.config import settings
    from app.utils.config_store import get
    from app.clients.paddle_classic import PaddleClassicClient
    from app.flows.customer_onboarding import checkout_email_key

    plan_map_raw = get("LAGO_PLAN_MAP")
    plans = json.loads(plan_map_raw) if plan_map_raw else []

    if not plans:
        raise HTTPException(status_code=404, detail="No plans configured. Run setup first.")

    r = redis_lib.from_url(settings.redis_url, decode_responses=True)
    email = r.get(checkout_email_key(external_id)) or ""
    passthrough = json.dumps({"lago_external_id": external_id})

    async def _redirect_to_paddle(paddle_plan_id: str) -> RedirectResponse:
        paddle = PaddleClassicClient()
        try:
            url = await paddle.generate_pay_link(
                product_id=paddle_plan_id,
                passthrough=passthrough,
                customer_email=email,
            )
        finally:
            await paddle.close()
        return RedirectResponse(url=url, status_code=302)

    # Plan selected via query param
    if plan:
        selected = next((p for p in plans if p["paddle_plan_id"] == plan), None)
        if not selected:
            raise HTTPException(status_code=404, detail="Plan not found")
        return await _redirect_to_paddle(selected["paddle_plan_id"])

    # Single plan configured — skip the picker
    if len(plans) == 1:
        return await _redirect_to_paddle(plans[0]["paddle_plan_id"])

    # Multiple plans — show picker
    return HTMLResponse(content=_render_plan_picker(external_id, plans))
