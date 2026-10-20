from fastapi import FastAPI

app = FastAPI(title="QueueLess")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}
