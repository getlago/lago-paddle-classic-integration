from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, RedirectResponse
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


@app.get("/checkout/{external_id:path}")
async def checkout_redirect(external_id: str):
    """
    Redirect to the Paddle checkout URL for this customer.
    The full URL is stored in Redis (Lago metadata has a 255-char value limit).
    """
    import redis as redis_lib
    from app.config import settings
    from app.flows.customer_onboarding import checkout_redis_key

    r = redis_lib.from_url(settings.redis_url, decode_responses=True)
    url = r.get(checkout_redis_key(external_id))
    if not url:
        raise HTTPException(status_code=404, detail="Checkout URL not found or expired")
    return RedirectResponse(url=url, status_code=302)
