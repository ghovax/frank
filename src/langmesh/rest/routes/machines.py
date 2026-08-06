"""Machine routes: the other machines this one can be sent to."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException

from langmesh.commons.services.broadcast import _publish_broadcast
from langmesh.commons.services.machines import (
    PairingLinkError,
    forget_machine,
    machine_address,
    machines_payload,
    remember_machine,
    rename_machine,
)
from langmesh.protocol.dtos import MachineNameRequest, MachineRequest

router = APIRouter()


@router.get("/machines")
async def list_machines():
    """Every machine this one knows how to reach. Without their tokens — see `/machines/{id}/address`."""
    return await asyncio.to_thread(machines_payload)


@router.post("/machines")
async def add_machine(request: MachineRequest):
    try:
        machine = await asyncio.to_thread(remember_machine, request.link)
    except PairingLinkError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    _publish_broadcast({"type": "machines_changed"})
    return machine


@router.get("/machines/{machine_id}/address")
async def machine_url(machine_id: str):
    """The URL that opens this machine, token included, separate from the list on purpose."""
    address = await asyncio.to_thread(machine_address, machine_id)
    if not address:
        raise HTTPException(status_code=404, detail="No such machine.")
    return {"url": address}


@router.put("/machines/{machine_id}")
async def name_machine(machine_id: str, request: MachineNameRequest):
    machine = await asyncio.to_thread(rename_machine, machine_id, request.name)
    if machine is None:
        raise HTTPException(status_code=404, detail="No such machine.")
    _publish_broadcast({"type": "machines_changed"})
    return machine


@router.delete("/machines/{machine_id}")
async def delete_machine(machine_id: str):
    if not await asyncio.to_thread(forget_machine, machine_id):
        raise HTTPException(status_code=404, detail="No such machine.")
    _publish_broadcast({"type": "machines_changed"})
    return {"ok": True}


__all__ = ["router"]
