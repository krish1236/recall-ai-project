from fastapi import FastAPI

from webhook import router as webhook_router

app = FastAPI(title="recall-ai api")

app.include_router(webhook_router)


@app.get("/health")
def health():
    return {"ok": True}
