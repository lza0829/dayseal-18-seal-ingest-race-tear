from fastapi import APIRouter, Body
from app.services import scope_service as svc

router = APIRouter(prefix="/settings", tags=["settings"])


@router.get("/close-scope")
def get_scope():
    return svc.get_scope()


@router.put("/close-scope")
def set_scope(body: dict = Body(...)):
    return svc.set_scope(body)
