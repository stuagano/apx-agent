from fastapi import FastAPI

from api import router

app = FastAPI(
    title="Account Search Service",
    description="Utility account search — VS fan-out + SQL fallback for entity resolution",
    version="0.1.0",
)
app.include_router(router)
