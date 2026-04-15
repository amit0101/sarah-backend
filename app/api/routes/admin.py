"""Developer admin API — org-scoped (multi-org revision §6)."""

from __future__ import annotations

import os
import tempfile
import uuid
from typing import Any, Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy import select

from app.api.auth import require_admin_key
from app.api.schemas import (
    LocationCreate,
    LocationUpdate,
    OrganizationCreate,
    OrganizationUpdate,
    PromptUpdate,
)
from app.database.session import DbSession
from app.ghl_client.client import GHLAPIError
from app.ghl_client.factory import clear_ghl_client_cache, effective_ghl_credentials, get_ghl_client_for_org
from app.ghl_client.pipelines import list_pipelines
from app.knowledge_base.crawler import crawl_site_to_vector_store
from app.knowledge_base.vector_store import VectorStoreService
from app.models.location import Location
from app.models.organization import Organization
from app.models.prompt import Prompt

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin_key)])


# --- Organizations ---


@router.get("/organizations")
async def list_organizations(db: DbSession) -> dict[str, Any]:
    r = await db.execute(select(Organization).order_by(Organization.name))
    rows = r.scalars().all()
    return {
        "organizations": [
            {
                "id": str(x.id),
                "name": x.name,
                "slug": x.slug,
                "status": x.status,
                "twilio_phone_number": x.twilio_phone_number,
            }
            for x in rows
        ]
    }


@router.post("/organizations")
async def create_organization(body: OrganizationCreate, db: DbSession) -> dict[str, Any]:
    org = Organization(
        id=uuid.uuid4(),
        name=body.name,
        slug=body.slug,
        status=body.status,
        ghl_api_key=body.ghl_api_key,
        ghl_location_id=body.ghl_location_id,
        vector_store_id=body.vector_store_id,
        twilio_phone_number=body.twilio_phone_number,
    )
    db.add(org)
    await db.commit()
    return {"id": str(org.id), "slug": org.slug}


@router.get("/organizations/{org_id}")
async def get_organization(org_id: uuid.UUID, db: DbSession) -> dict[str, Any]:
    org = await db.get(Organization, org_id)
    if not org:
        raise HTTPException(404, "not found")
    return {
        "id": str(org.id),
        "name": org.name,
        "slug": org.slug,
        "status": org.status,
        "ghl_location_id": org.ghl_location_id,
        "vector_store_id": org.vector_store_id,
        "twilio_phone_number": org.twilio_phone_number,
        "has_ghl_api_key": bool(org.ghl_api_key),
    }


@router.put("/organizations/{org_id}")
async def update_organization(org_id: uuid.UUID, body: OrganizationUpdate, db: DbSession) -> dict[str, str]:
    org = await db.get(Organization, org_id)
    if not org:
        raise HTTPException(404, "not found")
    data = body.model_dump(exclude_unset=True)
    for k, v in data.items():
        setattr(org, k, v)
    clear_ghl_client_cache(org_id)
    await db.commit()
    return {"status": "ok"}


@router.delete("/organizations/{org_id}")
async def delete_organization(org_id: uuid.UUID, db: DbSession) -> dict[str, str]:
    org = await db.get(Organization, org_id)
    if org:
        clear_ghl_client_cache(org_id)
        await db.delete(org)
        await db.commit()
    return {"status": "ok"}


@router.post("/organizations/{org_id}/test-ghl")
async def test_ghl(org_id: uuid.UUID, db: DbSession) -> dict[str, Any]:
    """Validate stored credentials against GHL (GET /opportunities/pipelines)."""
    org = await db.get(Organization, org_id)
    if not org:
        raise HTTPException(404, "not found")
    clear_ghl_client_cache(org_id)
    client = get_ghl_client_for_org(org, use_cache=False)
    _, loc = effective_ghl_credentials(org)
    if not loc:
        return {"ok": False, "error": "missing GHL_LOCATION_ID (org row or env)"}
    try:
        await list_pipelines(client, location_id=loc)
    except GHLAPIError as e:
        if e.status_code in (401, 403):
            return {"ok": False, "error": "authentication failed", "detail": str(e.body)}
        return {"ok": False, "error": str(e), "detail": str(e.body)}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True}


# --- Organization-level Knowledge Base ---


@router.post("/organizations/{org_id}/knowledge-base")
async def upload_org_kb(
    org_id: uuid.UUID,
    db: DbSession,
    file: UploadFile = File(...),
) -> dict[str, Any]:
    """Upload a file to the org-wide Vector Store (auto-creates if needed)."""
    org = await db.get(Organization, org_id)
    if not org:
        raise HTTPException(404, "organization not found")
    vss = VectorStoreService()
    if not org.vector_store_id:
        org.vector_store_id = await vss.create_vector_store(f"{org.slug}-knowledge")
        await db.flush()
    suffix = os.path.splitext(file.filename or "")[1] or ".txt"
    data = await file.read()
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(data)
        path = tmp.name
    try:
        fid = await vss.upload_file_to_vector_store(
            vector_store_id=org.vector_store_id,
            file_path=path,
            filename=file.filename or "upload",
        )
    finally:
        os.unlink(path)
    await db.commit()
    return {"file_id": fid, "vector_store_id": org.vector_store_id}


@router.get("/organizations/{org_id}/knowledge-base")
async def list_org_kb_files(org_id: uuid.UUID, db: DbSession) -> dict[str, Any]:
    """List all files in the org-wide Vector Store."""
    org = await db.get(Organization, org_id)
    if not org:
        raise HTTPException(404, "organization not found")
    if not org.vector_store_id:
        return {"vector_store_id": None, "files": []}
    vss = VectorStoreService()
    files = await vss.list_files_in_store(org.vector_store_id)
    return {"vector_store_id": org.vector_store_id, "files": files}


@router.delete("/organizations/{org_id}/knowledge-base/{file_id}")
async def delete_org_kb_file(
    org_id: uuid.UUID,
    file_id: str,
    db: DbSession,
) -> dict[str, str]:
    """Remove a file from the org-wide Vector Store."""
    org = await db.get(Organization, org_id)
    if not org or not org.vector_store_id:
        raise HTTPException(400, "organization or vector_store_id missing")
    vss = VectorStoreService()
    await vss.delete_file_from_store(org.vector_store_id, file_id)
    return {"status": "ok"}


# --- Locations (nested under org) ---


@router.get("/organizations/{org_id}/locations")
async def list_org_locations(org_id: uuid.UUID, db: DbSession) -> dict[str, Any]:
    r = await db.execute(select(Location).where(Location.organization_id == org_id))
    rows = r.scalars().all()
    return {
        "organization_id": str(org_id),
        "locations": [
            {"id": x.id, "name": x.name, "vector_store_id": x.vector_store_id} for x in rows
        ],
    }


@router.post("/organizations/{org_id}/locations")
async def create_org_location(org_id: uuid.UUID, body: LocationCreate, db: DbSession) -> dict[str, Any]:
    org = await db.get(Organization, org_id)
    if not org:
        raise HTTPException(404, "organization not found")
    loc = Location(
        organization_id=org.id,
        id=body.id,
        name=body.name,
        ghl_location_id=body.ghl_location_id,
        vector_store_id=body.vector_store_id,
        calendar_id=body.calendar_id,
        ghl_calendar_id=body.ghl_calendar_id,
        availability_calendar_id=body.availability_calendar_id,
        escalation_contacts=body.escalation_contacts,
        config=body.config,
    )
    db.add(loc)
    await db.commit()
    return {"organization_id": str(org_id), "location_id": loc.id}


@router.put("/organizations/{org_id}/locations/{location_slug}")
async def update_org_location(
    org_id: uuid.UUID,
    location_slug: str,
    body: LocationUpdate,
    db: DbSession,
) -> dict[str, str]:
    loc = await db.get(Location, (org_id, location_slug))
    if not loc:
        raise HTTPException(404, "not found")
    data = body.model_dump(exclude_unset=True)
    for k, v in data.items():
        setattr(loc, k, v)
    await db.commit()
    return {"status": "ok"}


@router.delete("/organizations/{org_id}/locations/{location_slug}")
async def delete_org_location(org_id: uuid.UUID, location_slug: str, db: DbSession) -> dict[str, str]:
    loc = await db.get(Location, (org_id, location_slug))
    if loc:
        await db.delete(loc)
        await db.commit()
    return {"status": "ok"}


@router.get("/organizations/{org_id}/locations/{location_slug}/config")
async def get_location_config(org_id: uuid.UUID, location_slug: str, db: DbSession) -> dict[str, Any]:
    loc = await db.get(Location, (org_id, location_slug))
    if not loc:
        raise HTTPException(404, "not found")
    return {"config": loc.config or {}}


@router.put("/organizations/{org_id}/locations/{location_slug}/config")
async def put_location_config(
    org_id: uuid.UUID,
    location_slug: str,
    body: dict[str, Any],
    db: DbSession,
) -> dict[str, str]:
    loc = await db.get(Location, (org_id, location_slug))
    if not loc:
        raise HTTPException(404, "not found")
    loc.config = body.get("config") or body
    await db.commit()
    return {"status": "ok"}


@router.get("/organizations/{org_id}/locations/{location_slug}/escalation")
async def get_location_escalation(org_id: uuid.UUID, location_slug: str, db: DbSession) -> dict[str, Any]:
    loc = await db.get(Location, (org_id, location_slug))
    if not loc:
        raise HTTPException(404, "not found")
    return {"escalation_contacts": loc.escalation_contacts or []}


@router.put("/organizations/{org_id}/locations/{location_slug}/escalation")
async def put_location_escalation(
    org_id: uuid.UUID,
    location_slug: str,
    body: dict[str, Any],
    db: DbSession,
) -> dict[str, str]:
    loc = await db.get(Location, (org_id, location_slug))
    if not loc:
        raise HTTPException(404, "not found")
    loc.escalation_contacts = body.get("escalation_contacts") or []
    await db.commit()
    return {"status": "ok"}


@router.post("/organizations/{org_id}/locations/{location_slug}/knowledge-base")
async def upload_kb(
    org_id: uuid.UUID,
    location_slug: str,
    db: DbSession,
    file: UploadFile = File(...),
) -> dict[str, Any]:
    loc = await db.get(Location, (org_id, location_slug))
    if not loc or not loc.vector_store_id:
        raise HTTPException(400, "location or vector_store_id missing")
    vss = VectorStoreService()
    suffix = os.path.splitext(file.filename or "")[1] or ".txt"
    data = await file.read()
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(data)
        path = tmp.name
    try:
        fid = await vss.upload_file_to_vector_store(
            vector_store_id=loc.vector_store_id,
            file_path=path,
            filename=file.filename or "upload",
        )
    finally:
        os.unlink(path)
    await db.commit()
    return {"file_id": fid}


@router.post("/organizations/{org_id}/locations/{location_slug}/knowledge-base/crawl")
async def crawl_kb(org_id: uuid.UUID, location_slug: str, db: DbSession) -> dict[str, Any]:
    loc = await db.get(Location, (org_id, location_slug))
    if not loc or not loc.vector_store_id:
        raise HTTPException(400, "location or vector_store_id missing")
    ids = await crawl_site_to_vector_store(vector_store_id=loc.vector_store_id)
    return {"uploaded_file_ids": ids}


@router.get("/organizations/{org_id}/locations/{location_slug}/knowledge-base")
async def list_kb_files(
    org_id: uuid.UUID,
    location_slug: str,
    db: DbSession,
) -> dict[str, Any]:
    """List all files in the location's Vector Store."""
    loc = await db.get(Location, (org_id, location_slug))
    if not loc or not loc.vector_store_id:
        raise HTTPException(400, "location or vector_store_id missing")
    vss = VectorStoreService()
    files = await vss.list_files_in_store(loc.vector_store_id)
    return {"vector_store_id": loc.vector_store_id, "files": files}


@router.delete("/organizations/{org_id}/locations/{location_slug}/knowledge-base/{file_id}")
async def delete_kb_file(
    org_id: uuid.UUID,
    location_slug: str,
    file_id: str,
    db: DbSession,
) -> dict[str, str]:
    """Remove a file from the location's Vector Store."""
    loc = await db.get(Location, (org_id, location_slug))
    if not loc or not loc.vector_store_id:
        raise HTTPException(400, "location or vector_store_id missing")
    vss = VectorStoreService()
    await vss.delete_file_from_store(loc.vector_store_id, file_id)
    return {"status": "ok"}


@router.get("/organizations/{org_id}/prompts/{path}")
async def get_prompt(
    org_id: uuid.UUID,
    path: str,
    db: DbSession,
    location_id: Optional[str] = None,
) -> dict[str, Any]:
    q = select(Prompt).where(Prompt.organization_id == org_id, Prompt.path == path)
    if location_id:
        q = q.where(Prompt.location_id == location_id)
    else:
        q = q.where(Prompt.location_id.is_(None))
    r = await db.execute(q)
    row = r.scalar_one_or_none()
    if not row:
        return {"path": path, "prompt": None}
    return {
        "path": path,
        "global_instructions": row.global_instructions,
        "path_instructions": row.path_instructions,
        "extra_config": row.extra_config,
    }


@router.put("/organizations/{org_id}/prompts/{path}")
async def put_prompt(
    org_id: uuid.UUID,
    path: str,
    body: PromptUpdate,
    db: DbSession,
    location_id: Optional[str] = None,
) -> dict[str, str]:
    q = select(Prompt).where(Prompt.organization_id == org_id, Prompt.path == path)
    if location_id:
        q = q.where(Prompt.location_id == location_id)
    else:
        q = q.where(Prompt.location_id.is_(None))
    r = await db.execute(q)
    row = r.scalar_one_or_none()
    if not row:
        row = Prompt(
            id=uuid.uuid4(),
            organization_id=org_id,
            location_id=location_id,
            path=path,
            global_instructions=body.global_instructions,
            path_instructions=body.path_instructions,
            extra_config=body.extra_config,
        )
        db.add(row)
    else:
        if body.global_instructions is not None:
            row.global_instructions = body.global_instructions
        if body.path_instructions is not None:
            row.path_instructions = body.path_instructions
        if body.extra_config is not None:
            row.extra_config = body.extra_config
    await db.commit()
    return {"status": "ok"}


@router.get("/health")
async def health(db: DbSession) -> dict[str, str]:
    from sqlalchemy import text

    try:
        await db.execute(text("SELECT 1"))
        return {"status": "ok", "database": "connected"}
    except Exception as e:
        return {"status": "degraded", "database": str(e)}
