from pathlib import Path
import asyncio

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from deepsearch.config import settings
from deepsearch.engine import SearchEngine
from deepsearch.models import IdentityReport, SearchRequest
from deepsearch.storage import Storage
from deepsearch.organizations import OrganizationEngine, OrganizationRequest


store = Storage(settings.data_dir)
engine = SearchEngine(settings, store)
app = FastAPI(title="Deep Web Search Tools", version="0.1.0")


static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=static_dir), name="static")


def authorize(x_api_key: str | None = Header(default=None)):
    if settings.app_api_key and x_api_key != settings.app_api_key:
        raise HTTPException(401, "Invalid API key")


@app.post('/api/organizations/discover', dependencies=[Depends(authorize)])
async def discover_organization(data: OrganizationRequest):
    try:
        return await asyncio.wait_for(OrganizationEngine(engine).run(data),150)
    except TimeoutError as exc:
        raise HTTPException(503,'Organization discovery reached its bounded time limit; retry the row') from exc


@app.get("/")
def dashboard():
    return FileResponse(static_dir / "index.html")


@app.get("/api/health")
def health():
    broad = bool(settings.brave_search_api_key if settings.search_provider == "brave"
                 else settings.searxng_url)
    return {"status": "ok", "provider": settings.search_provider,
            "configured": True, "keyless_company_search": True, "broad_search_configured": broad,
            "broad_provider": "searxng" if settings.search_provider == "auto" and settings.searxng_url
                              else settings.search_provider if broad else None}


@app.post("/api/search", response_model=IdentityReport, dependencies=[Depends(authorize)])
async def search(request: SearchRequest):
    try:
        return await engine.run(request)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/runs", response_model=list[IdentityReport], dependencies=[Depends(authorize)])
def runs():
    return store.recent_reports()


@app.get("/api/runs/{run_id}", response_model=IdentityReport, dependencies=[Depends(authorize)])
def run(run_id: str):
    result = store.get_report(run_id)
    if not result:
        raise HTTPException(404, "Run not found")
    return result


@app.delete("/api/runs/{run_id}", dependencies=[Depends(authorize)])
def delete_run(run_id: str):
    if not store.delete_report(run_id):
        raise HTTPException(404, "Run not found")
    return {"deleted": True}
