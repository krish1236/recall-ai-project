from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from admin import router as admin_router
from live import router as live_router
from meetings import router as meetings_router
from webhook import router as webhook_router

app = FastAPI(title="recall-ai api")

# Frontend runs on :3000 in dev; keep this permissive for local work.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(webhook_router)
app.include_router(meetings_router)
app.include_router(live_router)
app.include_router(admin_router)


@app.get("/health")
def health():
    return {"ok": True}
