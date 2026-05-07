from fastapi import FastAPI

from .router import router

app = FastAPI(
    title="AFR Enrollment API",
    description="Deterministic enrollment pipeline for AFR (Affordable Rate) applications",
    version="0.1.0",
)
app.include_router(router)
