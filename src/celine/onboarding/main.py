from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from celine.onboarding.config.settings import settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    Path(settings.data_dir).mkdir(parents=True, exist_ok=True)
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="CER Onboarding",
        version="0.1.0",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:3000"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    from celine.onboarding.api.health import router as health_router
    from celine.onboarding.api.submissions import router as submissions_router
    from celine.onboarding.api.documents import router as documents_router
    from celine.onboarding.api.extractions import router as extractions_router
    from celine.onboarding.api.consent_documents import router as consent_docs_router
    from celine.onboarding.api.config import router as config_router
    from celine.onboarding.api.eligibility import router as eligibility_router

    app.include_router(health_router, prefix="/api")
    app.include_router(config_router, prefix="/api")
    app.include_router(submissions_router, prefix="/api")
    app.include_router(documents_router, prefix="/api")
    app.include_router(extractions_router, prefix="/api")
    app.include_router(consent_docs_router, prefix="/api")
    app.include_router(eligibility_router, prefix="/api")

    return app


app = create_app()
