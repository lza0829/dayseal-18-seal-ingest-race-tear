from fastapi import APIRouter, Body
from app.services import close_service as svc

router = APIRouter(prefix="/close", tags=["close"])


@router.get("/preview")
def preview(business_day: str | None = None):
    return svc.preview(business_day)


@router.post("/seal")
def seal(body: dict = Body(...)):
    return svc.seal(body)


@router.get("/batches")
def batches():
    return svc.list_batches()


@router.get("/batches/{batch_id}")
def batch(batch_id: int):
    return svc.get_batch(batch_id)


@router.get("/variances")
def variances():
    return svc.list_variances()
