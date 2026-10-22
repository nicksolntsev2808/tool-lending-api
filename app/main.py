import os
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from typing import List, Literal, Optional

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Date, DateTime, ForeignKey, String, create_engine, func
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "mysql+pymysql://tools_user:tools_password@localhost:3306/tools_db",
)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

ToolStatus = Literal["available", "lent"]


class Base(DeclarativeBase):
    pass


class Tool(Base):
    __tablename__ = "tools"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    category: Mapped[str] = mapped_column(String(80), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="available", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Loan(Base):
    __tablename__ = "loans"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    tool_id: Mapped[int] = mapped_column(ForeignKey("tools.id"), nullable=False, index=True)
    borrower: Mapped[str] = mapped_column(String(100), nullable=False)
    lent_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    due_date: Mapped[date] = mapped_column(Date, nullable=False)
    returned_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)


class ToolCreate(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    category: Optional[str] = Field(default=None, max_length=80)


class ToolOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    category: Optional[str]
    status: str
    created_at: datetime


class LendRequest(BaseModel):
    borrower: str = Field(min_length=1, max_length=100)
    due_date: date


class LoanOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    tool_id: int
    borrower: str
    lent_at: datetime
    due_date: date
    returned_at: Optional[datetime]


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(title="Tool Lending API", version="1.0.0", lifespan=lifespan)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def get_tool_or_404(db: Session, tool_id: int) -> Tool:
    tool = db.get(Tool, tool_id)
    if not tool:
        raise HTTPException(status_code=404, detail="Tool not found")
    return tool


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/tools", response_model=ToolOut, status_code=201)
def create_tool(payload: ToolCreate, db: Session = Depends(get_db)) -> Tool:
    tool = Tool(name=payload.name, category=payload.category, status="available")
    db.add(tool)
    db.commit()
    db.refresh(tool)
    return tool


@app.get("/tools", response_model=List[ToolOut])
def list_tools(status: Optional[ToolStatus] = None, db: Session = Depends(get_db)) -> List[Tool]:
    query = db.query(Tool)
    if status:
        query = query.filter(Tool.status == status)
    return query.order_by(Tool.id).all()


@app.get("/tools/{tool_id}", response_model=ToolOut)
def get_tool(tool_id: int, db: Session = Depends(get_db)) -> Tool:
    return get_tool_or_404(db, tool_id)


@app.delete("/tools/{tool_id}", status_code=204)
def delete_tool(tool_id: int, db: Session = Depends(get_db)) -> None:
    tool = get_tool_or_404(db, tool_id)
    if tool.status == "lent":
        raise HTTPException(status_code=409, detail="Tool is lent out and cannot be deleted")
    db.query(Loan).filter(Loan.tool_id == tool_id).delete()
    db.delete(tool)
    db.commit()


@app.post("/tools/{tool_id}/lend", response_model=LoanOut, status_code=201)
def lend_tool(tool_id: int, payload: LendRequest, db: Session = Depends(get_db)) -> Loan:
    tool = get_tool_or_404(db, tool_id)
    if tool.status == "lent":
        raise HTTPException(status_code=409, detail="Tool is already lent out")
    if payload.due_date < date.today():
        raise HTTPException(status_code=400, detail="due_date cannot be in the past")

    loan = Loan(
        tool_id=tool.id,
        borrower=payload.borrower,
        lent_at=utc_now(),
        due_date=payload.due_date,
    )
    tool.status = "lent"
    db.add(loan)
    db.commit()
    db.refresh(loan)
    return loan


@app.post("/tools/{tool_id}/return", response_model=LoanOut)
def return_tool(tool_id: int, db: Session = Depends(get_db)) -> Loan:
    tool = get_tool_or_404(db, tool_id)
    if tool.status != "lent":
        raise HTTPException(status_code=409, detail="Tool is not lent out")

    loan = (
        db.query(Loan)
        .filter(Loan.tool_id == tool.id, Loan.returned_at.is_(None))
        .order_by(Loan.id.desc())
        .first()
    )
    loan.returned_at = utc_now()
    tool.status = "available"
    db.commit()
    db.refresh(loan)
    return loan


@app.get("/loans", response_model=List[LoanOut])
def list_loans(
    active: Optional[bool] = None,
    overdue: Optional[bool] = None,
    db: Session = Depends(get_db),
) -> List[Loan]:
    query = db.query(Loan)
    if active is True:
        query = query.filter(Loan.returned_at.is_(None))
    elif active is False:
        query = query.filter(Loan.returned_at.is_not(None))
    if overdue:
        query = query.filter(Loan.returned_at.is_(None), Loan.due_date < date.today())
    return query.order_by(Loan.id.desc()).all()
