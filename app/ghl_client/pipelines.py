"""Pipelines and opportunities — GHL API V2."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.ghl_client.client import GHLClient


async def list_pipelines(
    client: GHLClient,
    *,
    location_id: str,
) -> List[Dict[str, Any]]:
    data = await client.request(
        "GET",
        "/opportunities/pipelines",
        location_id=location_id,
        params={"locationId": location_id},
    )
    if isinstance(data, dict) and "pipelines" in data:
        return list(data["pipelines"])
    if isinstance(data, list):
        return data
    return []


async def create_opportunity(
    client: GHLClient,
    *,
    location_id: str,
    contact_id: str,
    pipeline_id: str,
    pipeline_stage_id: str,
    name: Optional[str] = None,
    monetary_value: Optional[float] = None,
    status: str = "open",
) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "locationId": location_id,
        "contactId": contact_id,
        "pipelineId": pipeline_id,
        "pipelineStageId": pipeline_stage_id,
        "status": status,
    }
    if name:
        body["name"] = name
    if monetary_value is not None:
        body["monetaryValue"] = monetary_value
    return await client.request(
        "POST",
        "/opportunities/",
        location_id=location_id,
        json_body=body,
    )


async def update_opportunity(
    client: GHLClient,
    opportunity_id: str,
    *,
    location_id: str,
    pipeline_stage_id: Optional[str] = None,
    status: Optional[str] = None,
    **extra: Any,
) -> Dict[str, Any]:
    body: Dict[str, Any] = {k: v for k, v in extra.items() if v is not None}
    if pipeline_stage_id:
        body["pipelineStageId"] = pipeline_stage_id
    if status:
        body["status"] = status
    return await client.request(
        "PUT",
        f"/opportunities/{opportunity_id}",
        location_id=location_id,
        json_body=body,
    )


async def search_opportunities(
    client: GHLClient,
    *,
    location_id: str,
    contact_id: str,
) -> List[Dict[str, Any]]:
    """Fetch opportunities for a contact (pagination simplified)."""
    data = await client.request(
        "GET",
        "/opportunities/search",
        location_id=location_id,
        params={"contact_id": contact_id, "location_id": location_id},
    )
    if isinstance(data, dict):
        opp = data.get("opportunities") or data.get("data")
        if isinstance(opp, list):
            return opp
    if isinstance(data, list):
        return data
    return []
