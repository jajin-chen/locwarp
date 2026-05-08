import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, UploadFile, File
from pydantic import BaseModel

from models.schemas import (
    Coordinate,
    RouteCategory,
    RouteMoveRequest,
    RoutePlanRequest,
    SavedRoute,
)
from services.route_service import RouteService
from services.gpx_service import GpxService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/route", tags=["route"])

route_service = RouteService()
gpx_service = GpxService()


def _rm():
    """Lazy lookup so route_store can read backend state without a circular
    import at module load (api/route is imported before main builds app_state)."""
    from main import app_state
    return app_state.route_manager


@router.post("/plan")
async def plan_route(req: RoutePlanRequest):
    profile_map = {"walking": "foot", "running": "foot", "driving": "car", "foot": "foot", "car": "car"}
    profile = profile_map.get(req.profile, "foot")
    result = await route_service.get_route(req.start.lat, req.start.lng, req.end.lat, req.end.lng, profile)
    return result


# ── Saved routes ──────────────────────────────────────────

@router.get("/saved", response_model=list[SavedRoute])
async def list_saved():
    return _rm().list_routes()


@router.post("/saved", response_model=SavedRoute)
async def save_route(route: SavedRoute):
    return _rm().create_route(route)


@router.put("/saved/{route_id}", response_model=SavedRoute)
async def replace_saved(route_id: str, route: SavedRoute):
    """Overwrite an existing saved route's payload.

    Backend half of the "save and overwrite same-named route" UX. Keeps
    the original id and created_at; updates name, waypoints, profile,
    and category, and stamps updated_at.
    """
    updated = _rm().replace_route(route_id, route)
    if updated is None:
        raise HTTPException(status_code=404, detail="Route not found")
    return updated


@router.delete("/saved/{route_id}")
async def delete_saved(route_id: str):
    if not _rm().delete_route(route_id):
        raise HTTPException(status_code=404, detail="Route not found")
    return {"status": "deleted"}


class _RouteRenameRequest(BaseModel):
    name: str


@router.patch("/saved/{route_id}")
async def rename_saved(route_id: str, req: _RouteRenameRequest):
    name = req.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail={"code": "invalid_name", "message": "路線名稱不可為空"})
    updated = _rm().rename_route(route_id, name)
    if updated is None:
        raise HTTPException(status_code=404, detail="Route not found")
    return updated


@router.post("/saved/move")
async def move_saved_routes(req: RouteMoveRequest):
    count = _rm().move_routes(req.route_ids, req.target_category_id)
    return {"moved": count}


@router.get("/saved/export")
async def export_all_saved_routes():
    """Export every saved route + categories as a single JSON bundle."""
    from fastapi.responses import Response
    body = _rm().export_json()
    return Response(content=body, media_type="application/json",
                    headers={"Content-Disposition": 'attachment; filename="locwarp-routes.json"'})


class _RouteImportBody(BaseModel):
    # Accept both the new bundle shape (categories + routes) and the
    # legacy shape (routes only). The manager copes with either.
    routes: list[SavedRoute] = []
    categories: list[RouteCategory] = []


@router.post("/saved/import")
async def import_all_saved_routes(body: _RouteImportBody):
    import json as _json
    payload = _json.dumps({
        "routes": [r.model_dump(mode="json") for r in body.routes],
        "categories": [c.model_dump(mode="json") for c in body.categories],
    })
    imported = _rm().import_json(payload)
    return {"imported": imported}


# ── Categories ────────────────────────────────────────────

@router.get("/categories", response_model=list[RouteCategory])
async def list_route_categories():
    return _rm().list_categories()


@router.post("/categories", response_model=RouteCategory)
async def create_route_category(cat: RouteCategory):
    return _rm().create_category(name=cat.name, color=cat.color)


@router.put("/categories/{cat_id}", response_model=RouteCategory)
async def update_route_category(cat_id: str, cat: RouteCategory):
    updated = _rm().update_category(cat_id, name=cat.name, color=cat.color)
    if not updated:
        raise HTTPException(status_code=404, detail="Category not found")
    return updated


@router.delete("/categories/{cat_id}")
async def delete_route_category(cat_id: str):
    if cat_id == "default":
        raise HTTPException(status_code=400, detail="Cannot delete default category")
    if not _rm().delete_category(cat_id):
        raise HTTPException(status_code=404, detail="Category not found")
    return {"status": "deleted"}


# ── GPX ───────────────────────────────────────────────────

@router.post("/gpx/import")
async def import_gpx(file: UploadFile = File(...)):
    content = await file.read()
    text = content.decode("utf-8")
    coords = gpx_service.parse_gpx(text)
    raw_name = file.filename or "Imported GPX"
    base_name = raw_name.rsplit(".", 1)[0] if raw_name.lower().endswith(".gpx") else raw_name
    route = SavedRoute(
        name=base_name or "Imported GPX",
        waypoints=coords,
        profile="walking",
    )
    saved = _rm().create_route(route)
    return {"status": "imported", "id": saved.id, "points": len(coords)}


@router.get("/gpx/export/{route_id}")
async def export_gpx(route_id: str):
    route = next((r for r in _rm().list_routes() if r.id == route_id), None)
    if route is None:
        raise HTTPException(status_code=404, detail="Route not found")
    points = [{"lat": c.lat, "lng": c.lng} for c in route.waypoints]
    gpx_xml = gpx_service.generate_gpx(points, name=route.name)
    from fastapi.responses import Response
    import urllib.parse
    safe_name = "".join(ch if ord(ch) < 128 and ch not in '"\\/' else "_" for ch in route.name) or "route"
    utf8_encoded = urllib.parse.quote(f"{route.name}.gpx", safe="")
    disposition = f'attachment; filename="{safe_name}.gpx"; filename*=UTF-8\'\'{utf8_encoded}'
    return Response(content=gpx_xml, media_type="application/gpx+xml",
                    headers={"Content-Disposition": disposition})
