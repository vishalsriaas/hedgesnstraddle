from typing import Optional
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from app.database import get_db
from app.models.schema import User, ConfigAuditLog
from app.core.auth import get_current_user

router = APIRouter(prefix="/api/v1/audit", tags=["Audit Logs"])

@router.get("/logs")
def get_config_audit_logs(
    config_type: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=1000),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    query = db.query(ConfigAuditLog)
    if config_type:
        query = query.filter(ConfigAuditLog.config_type == config_type.upper())
    
    logs = query.order_by(ConfigAuditLog.id.desc()).limit(limit).all()

    return [
        {
            "id": log.id,
            "user_email": log.user_email,
            "config_type": log.config_type,
            "field_name": log.field_name,
            "old_value": log.old_value,
            "new_value": log.new_value,
            "apply_mode": log.apply_mode,
            "status": log.status,
            "ip_address": log.ip_address,
            "created_at": log.created_at.strftime("%Y-%m-%d %H:%M:%S") if log.created_at else ""
        }
        for log in logs
    ]
