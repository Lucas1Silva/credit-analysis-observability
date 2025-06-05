"""
API FastAPI - Sistema de Crédito Corporativo
Recebe solicitações de análise e dispara SAGA
"""
import time
from contextlib import asynccontextmanager
from typing import List, Dict, Any
from uuid import UUID

import structlog
from fastapi import FastAPI, HTTPException, Depends, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from prometheus_fastapi_instrumentator import Instrumentator
from prometheus_client import Counter, Histogram, Gauge
from sqlalchemy.orm import Session

from .database import get_db, engine
from .saga_client import SAGAClient
from ..shared.models import (
    CreditRequest, CreditResponse, AnalysisStatus,
    CreditAnalysis, Company, Base
)

# Configurar logger estruturado
logger = structlog.get_logger()

# Métricas Prometheus customizadas
ANALYSIS_REQUESTS = Counter(
    'credit_analysis_requests_total',
    'Total de solicitações de análise',
    ['analysis_type', 'company_segment']
)

ANALYSIS_DURATION = Histogram(
    'credit_analysis_duration_seconds',
    'Duração das análises de crédito',
    ['analysis_type', 'status'],
    buckets=[0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0, 300.0]
)

ANALYSIS_AMOUNT = Histogram(
    'credit_analysis_amount_processed',
    'Valores processados nas análises',
    ['company_segment', 'status'],
    buckets=[1000, 10000, 100000, 1000000, 10000000, 100000000]
)

ACTIVE_ANALYSES = Gauge(
    'credit_analyses_active_count',
    'Número de análises ativas no momento'
)

ANALYSIS_ERRORS = Counter(
    'credit_analysis_errors_total',
    'Total de erros nas análises',
    ['error_type', 'step']
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifecycle da aplicação"""
    logger.info("Iniciando API de Crédito Corporativo")
    
    # Criar tabelas se não existirem
    Base.metadata.create_all(bind=engine)
    
    # Inicializar cliente SAGA
    app.state.saga_client = SAGAClient()
    
    yield
    
    logger.info("Finalizando API de Crédito Corporativo")


# Criar aplicação FastAPI
app = FastAPI(
    title="Sistema de Crédito Corporativo",
    description="API para análise de crédito empresarial com observabilidade completa",
    version="1.0.0",
    lifespan=lifespan
)

# CORS para desenvolvimento
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Instrumentação Prometheus
instrumentator = Instrumentator(
    should_group_status_codes=False,
    should_ignore_untemplated=True,
    should_respect_env_var=True,
    should_instrument_requests_inprogress=True,
    excluded_handlers=["/health", "/metrics"],
    env_var_name="ENABLE_METRICS",
    inprogress_name="http_requests_inprogress",
    inprogress_labels=True,
)

instrumentator.instrument(app).expose(app)


# === ROTAS PRINCIPAIS ===

@app.get("/health")
async def health_check():
    """Health check para load balancers"""
    return {
        "status": "healthy",
        "timestamp": time.time(),
        "service": "credit-api"
    }


@app.post("/credit/analyze", response_model=CreditResponse)
async def request_credit_analysis(
    request: CreditRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db)
):
    """
    Solicita análise de crédito corporativo
    
    - **company_cnpj**: CNPJ da empresa
    - **requested_amount**: Valor em centavos
    - **analysis_type**: standard, express ou complex
    - **priority**: Se deve ser processada prioritariamente
    """
    start_time = time.time()
    
    try:
        # Buscar ou criar empresa
        company = db.query(Company).filter(
            Company.cnpj == request.company_cnpj
        ).first()
        
        if not company:
            # Criar empresa básica (em produção viria de cadastro)
            company = Company(
                cnpj=request.company_cnpj,
                name=f"Empresa {request.company_cnpj}",
                segment="Unknown"
            )
            db.add(company)
            db.commit()
            db.refresh(company)
        
        # Criar análise
        analysis = CreditAnalysis(
            company_id=company.id,
            requested_amount=request.requested_amount,
            analysis_type=request.analysis_type.value,
            status=AnalysisStatus.PENDING.value,
            current_step="validation",
            saga_state={}
        )
        
        db.add(analysis)
        db.commit()
        db.refresh(analysis)
        
        # Métricas
        ANALYSIS_REQUESTS.labels(
            analysis_type=request.analysis_type.value,
            company_segment=company.segment or "unknown"
        ).inc()
        
        ACTIVE_ANALYSES.inc()
        
        # Log estruturado
        logger.info(
            "credit_analysis_requested",
            analysis_id=str(analysis.id),
            company_cnpj=request.company_cnpj,
            amount=request.requested_amount,
            type=request.analysis_type.value
        )
        
        # Disparar SAGA em background
        background_tasks.add_task(
            start_saga_analysis,
            analysis.id,
            request,
            company
        )
        
        # Resposta imediata
        return CreditResponse(
            analysis_id=analysis.id,
            status=AnalysisStatus.PENDING,
            company_cnpj=company.cnpj,
            requested_amount=request.requested_amount,
            created_at=analysis.created_at
        )
        
    except Exception as e:
        ANALYSIS_ERRORS.labels(
            error_type=type(e).__name__,
            step="request_validation"
        ).inc()
        
        logger.error(
            "credit_analysis_request_failed",
            error=str(e),
            company_cnpj=request.company_cnpj
        )
        
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/credit/analysis/{analysis_id}", response_model=CreditResponse)
async def get_analysis_status(
    analysis_id: UUID,
    db: Session = Depends(get_db)
):
    """Consulta status de uma análise específica"""
    
    analysis = db.query(CreditAnalysis).filter(
        CreditAnalysis.id == analysis_id
    ).first()
    
    if not analysis:
        raise HTTPException(status_code=404, detail="Análise não encontrada")
    
    company = db.query(Company).filter(
        Company.id == analysis.company_id
    ).first()
    
    # Calcular tempo de processamento se completada
    processing_time = None
    if analysis.completed_at and analysis.created_at:
        processing_time = (analysis.completed_at - analysis.created_at).total_seconds()
    
    return CreditResponse(
        analysis_id=analysis.id,
        status=AnalysisStatus(analysis.status),
        company_cnpj=company.cnpj,
        requested_amount=analysis.requested_amount,
        approved_amount=analysis.approved_amount,
        risk_score=analysis.risk_score,
        decision_reason=analysis.decision_reason,
        processing_time=processing_time,
        created_at=analysis.created_at,
        completed_at=analysis.completed_at
    )


@app.get("/credit/analyses", response_model=List[CreditResponse])
async def list_analyses(
    limit: int = 50,
    offset: int = 0,
    status: AnalysisStatus = None,
    db: Session = Depends(get_db)
):
    """Lista análises com paginação e filtros"""
    
    query = db.query(CreditAnalysis, Company).join(
        Company, CreditAnalysis.company_id == Company.id
    )
    
    if status:
        query = query.filter(CreditAnalysis.status == status.value)
    
    analyses = query.offset(offset).limit(limit).all()
    
    results = []
    for analysis, company in analyses:
        processing_time = None
        if analysis.completed_at and analysis.created_at:
            processing_time = (analysis.completed_at - analysis.created_at).total_seconds()
        
        results.append(CreditResponse(
            analysis_id=analysis.id,
            status=AnalysisStatus(analysis.status),
            company_cnpj=company.cnpj,
            requested_amount=analysis.requested_amount,
            approved_amount=analysis.approved_amount,
            risk_score=analysis.risk_score,
            decision_reason=analysis.decision_reason,
            processing_time=processing_time,
            created_at=analysis.created_at,
            completed_at=analysis.completed_at
        ))
    
    return results


@app.get("/metrics/business")
async def get_business_metrics(db: Session = Depends(get_db)):
    """Métricas de negócio em tempo real"""
    
    # Total de análises por status
    status_counts = {}
    for status in AnalysisStatus:
        count = db.query(CreditAnalysis).filter(
            CreditAnalysis.status == status.value
        ).count()
        status_counts[status.value] = count
    
    # Volume total processado hoje
    from sqlalchemy import func, and_
    from datetime import datetime, timedelta
    
    today = datetime.utcnow().date()
    total_volume = db.query(
        func.sum(CreditAnalysis.requested_amount)
    ).filter(
        and_(
            CreditAnalysis.created_at >= today,
            CreditAnalysis.created_at < today + timedelta(days=1)
        )
    ).scalar() or 0
    
    # Taxa de aprovação
    approved_count = status_counts.get('approved', 0)
    total_count = sum(status_counts.values())
    approval_rate = (approved_count / total_count * 100) if total_count > 0 else 0
    
    return {
        "status_distribution": status_counts,
        "total_volume_today": total_volume,
        "approval_rate_percent": round(approval_rate, 2),
        "active_analyses": status_counts.get('processing', 0),
        "timestamp": time.time()
    }


# === FUNÇÕES AUXILIARES ===

async def start_saga_analysis(analysis_id: UUID, request: CreditRequest, company: Company):
    """Inicia o processo SAGA para análise de crédito"""
    
    try:
        saga_client = app.state.saga_client
        
        # Dados para o SAGA
        analysis_data = {
            "analysis_id": str(analysis_id),
            "company_cnpj": request.company_cnpj,
            "company_segment": company.segment,
            "requested_amount": request.requested_amount,
            "analysis_type": request.analysis_type.value,
            "priority": request.priority,
            "metadata": request.metadata
        }
        
        # Iniciar SAGA
        await saga_client.start_analysis(analysis_data)
        
        logger.info(
            "saga_analysis_started",
            analysis_id=str(analysis_id),
            type=request.analysis_type.value
        )
        
    except Exception as e:
        ANALYSIS_ERRORS.labels(
            error_type=type(e).__name__,
            step="saga_start"
        ).inc()
        
        logger.error(
            "saga_analysis_start_failed",
            analysis_id=str(analysis_id),
            error=str(e)
        )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)