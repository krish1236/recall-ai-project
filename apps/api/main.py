from fastapi import FastAPI

app = FastAPI(title="recall-ai api")


@app.get("/health")
def health():
    return {"ok": True}
