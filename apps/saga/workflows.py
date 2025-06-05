"""
SAGA Workflows - Workers para análise de crédito
Cada função representa um passo da orquestração
"""
import time
import random
from datetime import datetime
from typing import Dict, Any
from uuid import UUID

import structlog
from celery import Celery
from prometheus_client import Counter, Histogram

from ..shared.models import MESARequest, MESAResponse
from ..api.database import db_transaction, log_audit_event
from .mesa_client import MESAClient

logger = structlog.get_logger()

# Configuração Celery
app = Celery('credit_saga')

# Métricas específicas dos workers
WORKER_TASKS_TOTAL = Counter(
    'worker_tasks_total',
    'Total de tarefas executadas pelos workers',
    ['worker_name', 'status']
)

WORKER_DURATION = Histogram(
    'worker_task_duration_seconds',
    'Duração das tarefas dos workers',
    ['worker_name'],
    buckets=[0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0]
)

# Cliente MESA compartilhado
mesa_client = MESAClient()


@app.task(bind=True, name='apps.saga.workflows.validate_request')
def validate_request(self, analysis_id: str, data: Dict[str, Any], step_name: str = "validation"):
    """
    PASSO 1: Validação inicial da solicitação
    - Valida CNPJ
    - Verifica valor mínimo/máximo
    - Valida dados da empresa
    """
    start_time = time.time()
    
    try:
        logger.info("validation_started", analysis_id=analysis_id)
        
        # Log de auditoria
        with db_transaction() as db:
            log_audit_event(
                db, analysis_id, "step_start", 
                {"step": step_name, "input_data": data}, step_name
            )
        
        # Simular validações
        company_cnpj = data.get("company_cnpj", "")
        requested_amount = data.get("requested_amount", 0)
        
        # Validação CNPJ (simplificada)
        if len(company_cnpj.replace(".", "").replace("/", "").replace("-", "")) != 14:
            raise ValueError("CNPJ inválido")
        
        # Validação valores
        if requested_amount < 100000:  # < R$ 1.000
            raise ValueError("Valor abaixo do mínimo")
        
        if requested_amount > 100000000000:  # > R$ 1 bilhão
            raise ValueError("Valor acima do máximo")
        
        # Simular tempo de processamento (0.2-0.8s)
        time.sleep(random.uniform(0.2, 0.8))
        
        result = {
            "validation_passed": True,
            "company_cnpj_valid": True,
            "amount_valid": True,
            "validation_timestamp": datetime.utcnow().isoformat()
        }
        
        # Log sucesso
        with db_transaction() as db:
            log_audit_event(
                db, analysis_id, "step_complete", 
                {"step": step_name, "result": result}, step_name
            )
        
        WORKER_TASKS_TOTAL.labels(worker_name="validation", status="success").inc()
        WORKER_DURATION.labels(worker_name="validation").observe(time.time() - start_time)
        
        # Notificar SAGA Manager
        from ..api.saga_client import SAGAClient
        saga_client = SAGAClient()
        saga_client.step_completed(analysis_id, step_name, result)
        
        logger.info("validation_completed", analysis_id=analysis_id, result=result)
        return result
        
    except Exception as e:
        WORKER_TASKS_TOTAL.labels(worker_name="validation", status="error").inc()
        
        logger.error("validation_failed", analysis_id=analysis_id, error=str(e))
        
        from ..api.saga_client import SAGAClient
        saga_client = SAGAClient()
        saga_client.step_failed(analysis_id, step_name, str(e))
        
        raise


@app.task(bind=True, name='apps.saga.workflows.check_limits')
def check_limits(self, analysis_id: str, data: Dict[str, Any], step_name: str = "limits_check"):
    """
    PASSO 2: Verificação de limites de crédito
    - Consulta limites da empresa
    - Verifica hierarquia (holding/subsidiárias)
    - Calcula disponibilidade
    """
    start_time = time.time()
    
    try:
        logger.info("limits_check_started", analysis_id=analysis_id)
        
        with db_transaction() as db:
            log_audit_event(
                db, analysis_id, "step_start", 
                {"step": step_name}, step_name
            )
            
            # Buscar limites da empresa
            from ..shared.models import Company, CreditAnalysis, CompanyLimit
            
            analysis = db.query(CreditAnalysis).filter(
                CreditAnalysis.id == UUID(analysis_id)
            ).first()
            
            if not analysis:
                raise ValueError("Análise não encontrada")
            
            company = db.query(Company).filter(
                Company.id == analysis.company_id
            ).first()
            
            if not company:
                raise ValueError("Empresa não encontrada")
            
            # Buscar limite da empresa (simulação se não existir)
            limit = db.query(CompanyLimit).filter(
                CompanyLimit.company_id == company.id
            ).first()
            
            if not limit:
                # Criar limite baseado no revenue da empresa
                revenue = company.annual_revenue or 50000000
                if revenue < 100000000:
                    total_limit = 10000000  # R$ 10M
                elif revenue < 500000000:
                    total_limit = 50000000  # R$ 50M
                else:
                    total_limit = 100000000  # R$ 100M
                
                limit = CompanyLimit(
                    company_id=company.id,
                    limit_type="individual",
                    total_limit=total_limit,
                    used_limit=random.randint(0, total_limit // 3)
                )
                db.add(limit)
                db.commit()
                db.refresh(limit)
        
        # Simular tempo de consulta
        time.sleep(random.uniform(0.5, 1.5))
        
        requested_amount = data.get("requested_amount", 0)
        available_limit = limit.total_limit - limit.used_limit
        
        # Verificar disponibilidade
        limit_sufficient = requested_amount <= available_limit
        utilization_percent = (limit.used_limit / limit.total_limit) * 100
        
        result = {
            "limit_check_passed": limit_sufficient,
            "total_limit": limit.total_limit,
            "used_limit": limit.used_limit,
            "available_limit": available_limit,
            "requested_amount": requested_amount,
            "utilization_percent": round(utilization_percent, 2),
            "company_segment": company.segment
        }
        
        if not limit_sufficient:
            raise ValueError(f"Limite insuficiente. Disponível: R$ {available_limit/100:.2f}")
        
        with db_transaction() as db:
            log_audit_event(
                db, analysis_id, "step_complete", 
                {"step": step_name, "result": result}, step_name
            )
        
        WORKER_TASKS_TOTAL.labels(worker_name="limits_check", status="success").inc()
        WORKER_DURATION.labels(worker_name="limits_check").observe(time.time() - start_time)
        
        # Notificar SAGA Manager
        from ..api.saga_client import SAGAClient
        saga_client = SAGAClient()
        saga_client.step_completed(analysis_id, step_name, result)
        
        logger.info("limits_check_completed", analysis_id=analysis_id, result=result)
        return result
        
    except Exception as e:
        WORKER_TASKS_TOTAL.labels(worker_name="limits_check", status="error").inc()
        
        logger.error("limits_check_failed", analysis_id=analysis_id, error=str(e))
        
        from ..api.saga_client import SAGAClient
        saga_client = SAGAClient()
        saga_client.step_failed(analysis_id, step_name, str(e))
        
        raise


@app.task(bind=True, name='apps.saga.workflows.assess_risk')
def assess_risk(self, analysis_id: str, data: Dict[str, Any], step_name: str = "risk_assessment"):
    """
    PASSO 3: Avaliação de risco da empresa
    - Calcula score de risco
    - Verifica histórico
    - Analisa setor econômico
    """
    start_time = time.time()
    
    try:
        logger.info("risk_assessment_started", analysis_id=analysis_id)
        
        with db_transaction() as db:
            log_audit_event(
                db, analysis_id, "step_start", 
                {"step": step_name}, step_name
            )
        
        # Simular análise de risco (2-4 segundos)
        time.sleep(random.uniform(2.0, 4.0))
        
        # Fatores de risco simulados
        company_segment = data.get("company_segment", "unknown")
        requested_amount = data.get("requested_amount", 0)
        utilization_percent = data.get("utilization_percent", 0)
        
        # Calcular score base (0-1000)
        base_score = random.randint(200, 800)
        
        # Ajustes por segmento
        segment_adjustments = {
            "Technology": -50,
            "Manufacturing": 0,
            "Logistics": +30,
            "Financial": -30,
            "Retail": +20,
            "unknown": +100
        }
        
        risk_score = base_score + segment_adjustments.get(company_segment, 0)
        
        # Ajustes por valor
        if requested_amount > 50000000:  # > R$ 500k
            risk_score += 50
        
        # Ajustes por utilização
        if utilization_percent > 80:
            risk_score += 100
        elif utilization_percent > 50:
            risk_score += 50
        
        # Garantir range 0-1000
        risk_score = max(0, min(1000, risk_score))
        
        # Determinar nível de risco
        if risk_score <= 300:
            risk_level = "low"
        elif risk_score <= 700:
            risk_level = "medium"
        elif risk_score <= 900:
            risk_level = "high"
        else:
            risk_level = "critical"
        
        # Simular fatores de risco
        risk_factors = {
            "credit_history_score": random.randint(600, 900),
            "financial_stability": random.choice(["excellent", "good", "fair", "poor"]),
            "sector_risk": random.choice(["low", "medium", "high"]),
            "geographic_risk": "low",
            "regulatory_compliance": random.choice([True, False]),
            "fraud_indicators": random.randint(0, 3)
        }
        
        result = {
            "risk_score": risk_score,
            "risk_level": risk_level,
            "risk_factors": risk_factors,
            "assessment_timestamp": datetime.utcnow().isoformat(),
            "assessment_method": "automated_v1.0"
        }
        
        # Falhar se risco crítico
        if risk_score > 950:
            raise ValueError(f"Risco crítico detectado: score {risk_score}")
        
        with db_transaction() as db:
            log_audit_event(
                db, analysis_id, "step_complete", 
                {"step": step_name, "result": result}, step_name
            )
        
        WORKER_TASKS_TOTAL.labels(worker_name="risk_assessment", status="success").inc()
        WORKER_DURATION.labels(worker_name="risk_assessment").observe(time.time() - start_time)
        
        from ..api.saga_client import SAGAClient
        saga_client = SAGAClient()
        saga_client.step_completed(analysis_id, step_name, result)
        
        logger.info("risk_assessment_completed", analysis_id=analysis_id, 
                   risk_score=risk_score, risk_level=risk_level)
        return result
        
    except Exception as e:
        WORKER_TASKS_TOTAL.labels(worker_name="risk_assessment", status="error").inc()
        
        logger.error("risk_assessment_failed", analysis_id=analysis_id, error=str(e))
        
        from ..api.saga_client import SAGAClient
        saga_client = SAGAClient()
        saga_client.step_failed(analysis_id, step_name, str(e))
        
        raise


@app.task(bind=True, name='apps.saga.workflows.consult_mesa')
def consult_mesa(self, analysis_id: str, data: Dict[str, Any], step_name: str = "mesa_consultation"):
    """
    PASSO 4: Consulta ao MESA (sistema de decisão)
    - Envia dados via Kafka
    - Aguarda resposta
    - Processa decisão
    """
    start_time = time.time()
    
    try:
        logger.info("mesa_consultation_started", analysis_id=analysis_id)
        
        with db_transaction() as db:
            log_audit_event(
                db, analysis_id, "step_start", 
                {"step": step_name}, step_name
            )
        
        # Preparar dados para MESA
        mesa_request = MESARequest(
            analysis_id=UUID(analysis_id),
            company_cnpj=data.get("company_cnpj"),
            requested_amount=data.get("requested_amount"),
            company_data={
                "segment": data.get("company_segment"),
                "utilization_percent": data.get("utilization_percent", 0)
            },
            risk_factors=data.get("risk_factors", {})
        )
        
        # Enviar para MESA via Kafka
        mesa_response = await mesa_client.send_request(mesa_request)
        
        # Processar resposta
        result = {
            "mesa_decision": mesa_response.decision,
            "mesa_approved_amount": mesa_response.approved_amount,
            "mesa_risk_score": mesa_response.risk_score,
            "mesa_confidence": mesa_response.confidence,
            "mesa_processing_time": mesa_response.processing_time,
            "mesa_timestamp": mesa_response.timestamp.isoformat()
        }
        
        # Falhar se MESA rejeitou
        if mesa_response.decision == "rejected":
            raise ValueError("Análise rejeitada pelo MESA")
        
        with db_transaction() as db:
            log_audit_event(
                db, analysis_id, "step_complete", 
                {"step": step_name, "result": result}, step_name
            )
        
        WORKER_TASKS_TOTAL.labels(worker_name="mesa_consultation", status="success").inc()
        WORKER_DURATION.labels(worker_name="mesa_consultation").observe(time.time() - start_time)
        
        from ..api.saga_client import SAGAClient
        saga_client = SAGAClient()
        saga_client.step_completed(analysis_id, step_name, result)
        
        logger.info("mesa_consultation_completed", analysis_id=analysis_id, 
                   decision=mesa_response.decision)
        return result
        
    except Exception as e:
        WORKER_TASKS_TOTAL.labels(worker_name="mesa_consultation", status="error").inc()
        
        logger.error("mesa_consultation_failed", analysis_id=analysis_id, error=str(e))
        
        from ..api.saga_client import SAGAClient
        saga_client = SAGAClient()
        saga_client.step_failed(analysis_id, step_name, str(e))
        
        raise


@app.task(bind=True, name='apps.saga.workflows.make_decision')
def make_decision(self, analysis_id: str, data: Dict[str, Any], step_name: str = "decision_making"):
    """
    PASSO 5: Tomada de decisão final
    - Consolida resultados de todos os passos
    - Aplica regras de negócio
    - Define valor aprovado
    """
    start_time = time.time()
    
    try:
        logger.info("decision_making_started", analysis_id=analysis_id)
        
        with db_transaction() as db:
            log_audit_event(
                db, analysis_id, "step_start", 
                {"step": step_name}, step_name
            )
        
        # Consolidar dados de todos os passos
        requested_amount = data.get("requested_amount", 0)
        mesa_decision = data.get("mesa_decision", "rejected")
        mesa_approved_amount = data.get("mesa_approved_amount", 0)
        risk_score = data.get("risk_score", 1000)
        available_limit = data.get("available_limit", 0)
        
        # Regras de negócio
        if mesa_decision == "rejected":
            final_decision = "rejected"
            approved_amount = 0
            reason = "Rejeitado pelo sistema MESA"
        elif risk_score > 850:
            final_decision = "rejected"
            approved_amount = 0
            reason = f"Risco muito alto: score {risk_score}"
        elif mesa_approved_amount > available_limit:
            # Limitar ao disponível
            final_decision = "approved"
            approved_amount = available_limit
            reason = f"Aprovado com limite disponível: R$ {available_limit/100:.2f}"
        else:
            final_decision = "approved"
            approved_amount = mesa_approved_amount or requested_amount
            reason = "Aprovado conforme análise completa"
        
        # Simular tempo de decisão
        time.sleep(random.uniform(0.5, 1.0))
        
        result = {
            "status": final_decision,
            "approved_amount": approved_amount,
            "requested_amount": requested_amount,
            "risk_score": risk_score,
            "decision_reason": reason,
            "decision_timestamp": datetime.utcnow().isoformat(),
            "decision_method": "automated_with_mesa"
        }
        
        with db_transaction() as db:
            log_audit_event(
                db, analysis_id, "step_complete", 
                {"step": step_name, "result": result}, step_name
            )
        
        WORKER_TASKS_TOTAL.labels(worker_name="decision_making", status="success").inc()
        WORKER_DURATION.labels(worker_name="decision_making").observe(time.time() - start_time)
        
        from ..api.saga_client import SAGAClient
        saga_client = SAGAClient()
        saga_client.step_completed(analysis_id, step_name, result)
        
        logger.info("decision_making_completed", analysis_id=analysis_id, 
                   decision=final_decision, amount=approved_amount)
        return result
        
    except Exception as e:
        WORKER_TASKS_TOTAL.labels(worker_name="decision_making", status="error").inc()
        
        logger.error("decision_making_failed", analysis_id=analysis_id, error=str(e))
        
        from ..api.saga_client import SAGAClient
        saga_client = SAGAClient()
        saga_client.step_failed(analysis_id, step_name, str(e))
        
        raise


@app.task(bind=True, name='apps.saga.workflows.send_notification')
def send_notification(self, analysis_id: str, data: Dict[str, Any], step_name: str = "notification"):
    """
    PASSO 6: Notificação final
    - Envia resultado para cliente
    - Atualiza sistemas externos
    - Finaliza processo
    """
    start_time = time.time()
    
    try:
        logger.info("notification_started", analysis_id=analysis_id)
        
        with db_transaction() as db:
            log_audit_event(
                db, analysis_id, "step_start", 
                {"step": step_name}, step_name
            )
        
        # Preparar notificação
        company_cnpj = data.get("company_cnpj")
        status = data.get("status", "approved")
        approved_amount = data.get("approved_amount", 0)
        decision_reason = data.get("decision_reason", "")
        
        # Simular envio de notificação
        time.sleep(random.uniform(0.2, 0.5))
        
        notification_data = {
            "analysis_id": analysis_id,
            "company_cnpj": company_cnpj,
            "status": status,
            "approved_amount": approved_amount,
            "decision_reason": decision_reason,
            "notification_sent": True,
            "notification_timestamp": datetime.utcnow().isoformat(),
            "notification_channels": ["email", "api_callback"]
        }
        
        # Em produção: enviar email, SMS, webhook, etc
        logger.info("notification_sent", 
                   analysis_id=analysis_id, 
                   company_cnpj=company_cnpj,
                   status=status)
        
        result = notification_data
        
        with db_transaction() as db:
            log_audit_event(
                db, analysis_id, "step_complete", 
                {"step": step_name, "result": result}, step_name
            )
        
        WORKER_TASKS_TOTAL.labels(worker_name="notification", status="success").inc()
        WORKER_DURATION.labels(worker_name="notification").observe(time.time() - start_time)
        
        from ..api.saga_client import SAGAClient
        saga_client = SAGAClient()
        saga_client.step_completed(analysis_id, step_name, result)
        
        logger.info("notification_completed", analysis_id=analysis_id)
        return result
        
    except Exception as e:
        WORKER_TASKS_TOTAL.labels(worker_name="notification", status="error").inc()
        
        logger.error("notification_failed", analysis_id=analysis_id, error=str(e))
        
        from ..api.saga_client import SAGAClient
        saga_client = SAGAClient()
        saga_client.step_failed(analysis_id, step_name, str(e))
        
        raise


# === WORKERS PARA ANÁLISES COMPLEXAS ===

@app.task(bind=True, name='apps.saga.workflows.enhanced_verification')
def enhanced_verification(self, analysis_id: str, data: Dict[str, Any], step_name: str = "enhanced_verification"):
    """Verificação aprimorada para análises complexas"""
    start_time = time.time()
    
    try:
        logger.info("enhanced_verification_started", analysis_id=analysis_id)
        
        # Simular verificações extras (3-5s)
        time.sleep(random.uniform(3.0, 5.0))
        
        result = {
            "enhanced_checks_passed": True,
            "document_verification": "approved",
            "background_check": "clean",
            "regulatory_status": "compliant"
        }
        
        WORKER_TASKS_TOTAL.labels(worker_name="enhanced_verification", status="success").inc()
        WORKER_DURATION.labels(worker_name="enhanced_verification").observe(time.time() - start_time)
        
        from ..api.saga_client import SAGAClient
        saga_client = SAGAClient()
        saga_client.step_completed(analysis_id, step_name, result)
        
        return result
        
    except Exception as e:
        WORKER_TASKS_TOTAL.labels(worker_name="enhanced_verification", status="error").inc()
        
        from ..api.saga_client import SAGAClient
        saga_client = SAGAClient()
        saga_client.step_failed(analysis_id, step_name, str(e))
        
        raise


@app.task(bind=True, name='apps.saga.workflows.compliance_check')
def compliance_check(self, analysis_id: str, data: Dict[str, Any], step_name: str = "compliance_check"):
    """Verificação de compliance BACEN"""
    start_time = time.time()
    
    try:
        logger.info("compliance_check_started", analysis_id=analysis_id)
        
        # Simular verificações de compliance
        time.sleep(random.uniform(2.0, 3.0))
        
        result = {
            "bacen_compliance": True,
            "aml_check": "approved",
            "sanctions_check": "clear",
            "pep_check": "negative"
        }
        
        WORKER_TASKS_TOTAL.labels(worker_name="compliance_check", status="success").inc()
        WORKER_DURATION.labels(worker_name="compliance_check").observe(time.time() - start_time)
        
        from ..api.saga_client import SAGAClient
        saga_client = SAGAClient()
        saga_client.step_completed(analysis_id, step_name, result)
        
        return result
        
    except Exception as e:
        WORKER_TASKS_TOTAL.labels(worker_name="compliance_check", status="error").inc()
        
        from ..api.saga_client import SAGAClient
        saga_client = SAGAClient()
        saga_client.step_failed(analysis_id, step_name, str(e))
        
        raise