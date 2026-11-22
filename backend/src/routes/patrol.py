from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session
from src.config.database import connection
from src.constants.enums import Role, Status
from src.controllers.auth import allow, current
from src.domain.patrol import plan_patrol
from src.domain.demo import demo_patrol
from src.models.identity import User
from src.models.jobs import PatrolPlan, Run
from src.models.source import Dataset
from src.schemas.requests import PatrolRequest
from src.services.audit import record
from src.utils import views
from src.utils.exports import write_report

router = APIRouter(prefix='/runs')


def _require_complete(db, run_id):
    item = db.get(Run, run_id)
    if not item:
        raise HTTPException(404, '找不到该运行记录')
    if item.status != Status.COMPLETE or not item.result:
        raise HTTPException(409, '只有已完成的分析结果可以排班')
    return item


@router.get('/{run_id}/patrol')
def patrol_detail(run_id: int, db: Session = Depends(connection), actor: User = Depends(current)):
    _require_complete(db, run_id)
    latest = db.scalar(select(PatrolPlan).where(PatrolPlan.run_id == run_id)
                       .order_by(PatrolPlan.id.desc()).limit(1))
    return views.patrol(latest)


@router.post('/{run_id}/patrol', status_code=201)
def patrol_schedule(run_id: int, body: PatrolRequest, db: Session = Depends(connection),
                    actor: User = Depends(allow(Role.ADMIN, Role.ANALYST))):
    item = _require_complete(db, run_id)
    dataset = db.get(Dataset, item.dataset_id)
    if not dataset:
        raise HTTPException(409, '运行来源批次已不可用，请重新计算后再排班')
    plan = plan_patrol(dataset.rows, item.params, body.model_dump())
    saved = PatrolPlan(run_id=item.id, inputs=body.model_dump(), plan=plan, author=actor.username)
    db.add(saved)
    db.flush()
    # Refresh the CSV so the download always carries the latest patrol plan.
    item.export_file = write_report(item.id, max(item.attempt, 1), item.result, plan)
    record(db, actor.username, 'patrol.scheduled', item.id,
           state=plan['state'], covered=next((m['value'] for m in plan['metrics'] if m['label'] == '已覆盖窗口'), 0),
           conflicts=len(plan['conflicts']))
    db.commit()
    return views.patrol(saved)


@router.get('/{run_id}/patrol/sample')
def patrol_sample(run_id: int, db: Session = Depends(connection), actor: User = Depends(current)):
    item = _require_complete(db, run_id)
    result = item.result or {}
    days = []
    for table in result.get('tables', []):
        if table['title'] == '留出窗口核对':
            days = sorted({row.get('date') for row in table['rows'] if row.get('date')})
            break
    if not days:
        raise HTTPException(409, '该运行结果缺少留出窗口日期，无法生成示例底册')
    return demo_patrol(days)
