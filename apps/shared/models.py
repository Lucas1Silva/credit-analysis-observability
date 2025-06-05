"""
Modelos de dados compartilhados - Sistema de Crédito Corporativo
"""
from datetime import datetime
from enum import Enum
from typing import Optional, Dict, Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, validator
from sqlalchemy import (
    Column, String, Integer, BigInteger, DateTime, JSON, Text, Boolean
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.sql import func

Base = declarative_base()


class AnalysisStatus(str, Enum):
    """Status das análises de crédito"""
    PENDING = "pending"
    PROCESSING = "processing"
    APPROVED = "approved"
    REJECTED = "rejected"
    ERROR = "error"
    CANCELLED = "cancelled"


class AnalysisType(str, Enum):
    """Tipos de análise disponíveis"""
    STANDARD = "standard"      # Análise padrão (até R$ 10M)
    EXPRESS = "express"        # Análise rápida (até R$ 1M)
    COMPLEX = "complex"        # Análise complexa (acima R$ 10M)


class RiskLevel(str, Enum):
    """Níveis de risco"""
    LOW = "low"           # Score 0-300
    MEDIUM = "medium"     # Score 301-700
    HIGH = "high"         # Score 701-900
    CRITICAL = "critical" # Score 901-1000


# === PYDANTIC MODELS (API) ===

class CreditRequest(BaseModel):
    """Solicitação de análise de crédito"""
    company_cnpj: str = Field(..., description="CNPJ da empresa (formato: XX.XXX.XXX/XXXX-XX)")
    requested_amount: int = Field(..., gt=0, description="Valor solicitado em centavos")
    analysis_type: AnalysisType = Field(default=AnalysisType.STANDARD)
    priority: bool = Field(default=False, description="Análise prioritária")
    metadata: Optional[Dict[str, Any]] = Field(default_factory=dict)

    @validator("company_cnpj")
    def validate_cnpj(cls, v):
        # Remove formatação
        cnpj = "".join(filter(str.isdigit, v))
        if len(cnpj) != 14:
            raise ValueError("CNPJ deve ter 14 dígitos")
        return f"{cnpj[:2]}.{cnpj[2:5]}.{cnpj[5:8]}/{cnpj[8:12]}-{cnpj[12:]}"

    @validator("requested_amount")
    def validate_amount(cls, v):
        if v < 100000:  # Mínimo R$ 1.000,00
            raise ValueError("Valor mínimo: R$ 1.000,00")
        if v > 100000000000:  # Máximo R$ 1 bilhão
            raise ValueError("Valor máximo: R$ 1 bilhão")
        return v


class CreditResponse(BaseModel):
    """Resposta da análise de crédito"""
    analysis_id: UUID
    status: AnalysisStatus
    company_cnpj: str
    requested_amount: int
    approved_amount: Optional[int] = None
    risk_score: Optional[int] = None
    risk_level: Optional[RiskLevel] = None
    decision_reason: Optional[str] = None
    processing_time: Optional[float] = None  # segundos
    created_at: datetime
    completed_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class SAGAStep(BaseModel):
    """Passo individual do SAGA"""
    step_name: str
    status: str  # 'pending', 'running', 'completed', 'failed', 'compensated'
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


class SAGAState(BaseModel):
    """Estado completo do SAGA"""
    analysis_id: UUID
    current_step: str
    steps: Dict[str, SAGAStep] = Field(default_factory=dict)
    compensation_needed: bool = False
    metadata: Dict[str, Any] = Field(default_factory=dict)


# === SQLALCHEMY MODELS (Database) ===

class Company(Base):
    """Empresa no sistema"""
    __tablename__ = "companies"

    id = Column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    cnpj = Column(String(18), unique=True, nullable=False, index=True)
    name = Column(String(255), nullable=False)
    segment = Column(String(100))
    annual_revenue = Column(BigInteger)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class CreditAnalysis(Base):
    """Análise de crédito"""
    __tablename__ = "credit_analyses"

    id = Column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    company_id = Column(PGUUID(as_uuid=True), nullable=False)
    requested_amount = Column(BigInteger, nullable=False)
    analysis_type = Column(String(50), nullable=False)
    status = Column(String(50), nullable=False, default=AnalysisStatus.PENDING)
    current_step = Column(String(100))
    saga_state = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    completed_at = Column(DateTime(timezone=True))
    decision_reason = Column(Text)
    approved_amount = Column(BigInteger)
    risk_score = Column(Integer)
    processing_time = Column(Integer)  # milissegundos


class AnalysisLog(Base):
    """Log imutável de auditoria"""
    __tablename__ = "analysis_logs"
    __table_args__ = {"schema": "audit"}

    id = Column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    analysis_id = Column(PGUUID(as_uuid=True), nullable=False)
    event_type = Column(String(50), nullable=False)
    step_name = Column(String(100))
    event_data = Column(JSON, nullable=False)
    user_id = Column(String(100))
    timestamp = Column(DateTime(timezone=True), server_default=func.now())
    hash_previous = Column(String(64))
    hash_current = Column(String(64))


class BusinessMetric(Base):
    """Métricas de negócio agregadas"""
    __tablename__ = "business_metrics"
    __table_args__ = {"schema": "metrics"}

    id = Column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    metric_name = Column(String(100), nullable=False)
    metric_value = Column(BigInteger)  # Valores em centavos/inteiros
    metric_unit = Column(String(20))
    dimensions = Column(JSON)  # {"segment": "tech", "amount_range": "1M-10M"}
    timestamp = Column(DateTime(timezone=True), server_default=func.now())


# === KAFKA MESSAGES ===

class MESARequest(BaseModel):
    """Requisição para o MESA"""
    analysis_id: UUID
    company_cnpj: str
    requested_amount: int
    company_data: Dict[str, Any]
    risk_factors: Dict[str, Any]
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class MESAResponse(BaseModel):
    """Resposta do MESA"""
    analysis_id: UUID
    decision: str  # 'approved', 'rejected', 'manual_review'
    approved_amount: Optional[int] = None
    risk_score: int = Field(..., ge=0, le=1000)
    risk_factors: Dict[str, Any]
    confidence: float = Field(..., ge=0.0, le=1.0)
    processing_time: float
    timestamp: datetime = Field(default_factory=datetime.utcnow)


# === PROMETHEUS METRICS MODELS ===

class MetricPoint(BaseModel):
    """Ponto de métrica para Prometheus"""
    name: str
    value: float
    labels: Dict[str, str] = Field(default_factory=dict)
    timestamp: Optional[datetime] = None


class AnalysisMetrics(BaseModel):
    """Métricas de uma análise específica"""
    analysis_id: UUID
    duration_seconds: float
    amount_processed: int
    risk_score: int
    decision: str
    step_durations: Dict[str, float]  # {"validation": 0.5, "mesa": 2.3, ...}
    error_count: int = 0

class CompanyLimit(Base):
    """Limites de crédito por empresa"""
    __tablename__ = "company_limits"

    id = Column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    company_id = Column(PGUUID(as_uuid=True), nullable=False)
    parent_company_id = Column(PGUUID(as_uuid=True))  # Para hierarquia
    limit_type = Column(String(50), nullable=False)  # 'individual', 'consolidated'
    total_limit = Column(BigInteger, nullable=False)
    used_limit = Column(BigInteger, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    @property
    def available_limit(self):
        return self.total_limit - self.used_limit