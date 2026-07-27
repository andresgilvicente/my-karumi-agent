from fastapi import FastAPI


app = FastAPI(
    title="My Karumi Agent",
    description="Local backend for the Metric Master Suite product demo.",
    version="0.1.0",
)


@app.get("/health", tags=["system"])
async def health() -> dict[str, str]:
    return {"status": "ok"}
