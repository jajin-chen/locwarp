import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, UploadFile, File

from models.schemas import RoutePlanRequest, SavedRoute, Coordinate
from services.route_service import RouteService
from services.gpx_service import GpxService

router = APIRouter(prefix="/api/route", tags=["route"])

route_service = RouteService()
gpx_service = GpxService()

# In-memory saved routes (could persist to JSON later)
_saved_routes: dict[str, SavedRoute] = {}


@router.post("/plan")
async def plan_route(req: RoutePlanRequest):
    profile_map = {"walking": "foot", "running": "foot", "driving": "car", "foot": "foot", "car": "car"}
    profile = profile_map.get(req.profile, "foot")
    result = await route_service.get_route(req.start.lat, req.start.lng, req.end.lat, req.end.lng, profile)
    return result


@router.get("/saved", response_model=list[SavedRoute])
async def list_saved():
    return list(_saved_routes.values())


@router.post("/saved", response_model=SavedRoute)
async def save_route(route: SavedRoute):
    route.id = str(uuid.uuid4())
    route.created_at = datetime.now(timezone.utc).isoformat()
    _saved_routes[route.id] = route
    return route


@router.delete("/saved/{route_id}")
async def delete_saved(route_id: str):
    if route_id not in _saved_routes:
        raise HTTPException(status_code=404, detail="Route not found")
    del _saved_routes[route_id]
    return {"status": "deleted"}


from pydantic import BaseModel as _BM


class _RouteRenameRequest(_BM):
    name: str


@router.patch("/saved/{route_id}")
async def rename_saved(route_id: str, req: _RouteRenameRequest):
    if route_id not in _saved_routes:
        raise HTTPException(status_code=404, detail="Route not found")
    name = req.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail={"code": "invalid_name", "message": "路線名稱不可為空"})
    _saved_routes[route_id].name = name
    return _saved_routes[route_id]


@router.post("/gpx/import")
async def import_gpx(file: UploadFile = File(...)):
    content = await file.read()
    text = content.decode("utf-8")
    coords = gpx_service.parse_gpx(text)
    # Strip the .gpx extension from the filename so the rename input
    # doesn't show "myroute.gpx" — the format suffix is irrelevant to the
    # in-app route name.
    raw_name = file.filename or "Imported GPX"
    base_name = raw_name.rsplit(".", 1)[0] if raw_name.lower().endswith(".gpx") else raw_name
    route = SavedRoute(
        id=str(uuid.uuid4()),
        name=base_name or "Imported GPX",
        waypoints=coords,
        profile="walking",
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    _saved_routes[route.id] = route
    return {"status": "imported", "id": route.id, "points": len(coords)}


@router.get("/gpx/export/{route_id}")
async def export_gpx(route_id: str):
    if route_id not in _saved_routes:
        raise HTTPException(status_code=404, detail="Route not found")
    route = _saved_routes[route_id]
    points = [{"lat": c.lat, "lng": c.lng} for c in route.waypoints]
    gpx_xml = gpx_service.generate_gpx(points, name=route.name)
    from fastapi.responses import Response
    return Response(content=gpx_xml, media_type="application/gpx+xml",
                    headers={"Content-Disposition": f'attachment; filename="{route.name}.gpx"'})
