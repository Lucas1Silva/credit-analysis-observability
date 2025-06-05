"""
Cliente SAGA para comunicação com Celery Workers
Orquestra análises de crédito distribuídas
"""
import os
import asyncio
from typing import Dict, Any, Optional
from uuid import UUID
import json

import structlog
from celery import Celery
from redis import Redis
from prometheus_client import Counter, Histogram, Gauge

from ..shared.models import SAGAState, SAGAStep, AnalysisStatus

logger = structlog.get_logger()

# Configurações
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
BROKER_URL = os.getenv("BROKER_URL", "redis://redis:6379/1")
RESULT_BACKEND = os.getenv("RESULT_BACKEND", "redis://redis:6379/2")

# Métricas Prometheus
SAGA_STEPS_TOTAL = Counter(
    'saga_steps_total',
    'Total de passos SAGA executados',
    ['step_name', 'status']
)

SAGA_DURATION = Histogram(
    'saga_step_duration_seconds',
    'Duração dos passos SAGA',
    ['step_name'],
    buckets=[0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0]
)

SAGA_ACTIVE = Gauge(
    'saga_analyses_active',
    'Número de SAGAs ativos'
)

SAGA_ERRORS = Counter(
    'saga_errors_total',
    'Erros nos SAGAs',
    ['step_name', 'error_type']
)


class SAGAClient:
    """Cliente para gerenciar SAGAs de análise de crédito"""
    
    def __init__(self):
        # Cliente Redis para estado
        self.redis = Redis.from_url(REDIS_URL, decode_responses=True)
        
        # Cliente Celery para workers
        self.celery = Celery(
            'credit_saga',
            broker=BROKER_URL,
            backend=RESULT_BACKEND,
            include=['apps.saga.workflows']
        )
        
        # Configurações Celery otimizadas para banking
        self.celery.conf.update(
            task_serializer='json',
            accept_content=['json'],
            result_serializer='json',
            timezone='UTC',
            enable_utc=True,
            task_track_started=True,
            task_time_limit=300,      # 5 minutos máximo por tarefa
            task_soft_time_limit=240, # Warning aos 4 minutos
            worker_prefetch_multiplier=1,
            task_acks_late=True,
            worker_disable_rate_limits=False,
            task_compression='gzip',
            result_compression='gzip',
            result_expires=3600,      # Resultados expiram em 1h
        )
        
        logger.info("saga_client_initialized")
    
    async def start_analysis(self, analysis_data: Dict[str, Any]) -> str:
        """
        Inicia um novo SAGA de análise de crédito
        
        Args:
            analysis_data: Dados da análise (ID, CNPJ, valor, etc)
            
        Returns:
            saga_id: ID único do SAGA
        """
        analysis_id = analysis_data["analysis_id"]
        
        try:
            # Definir passos baseado no tipo de análise
            analysis_type = analysis_data.get("analysis_type", "standard")
            steps = self._get_analysis_steps(analysis_type)
            
            # Estado inicial do SAGA
            saga_state = SAGAState(
                analysis_id=UUID(analysis_id),
                current_step=steps[0],
                steps={
                    step: SAGAStep(step_name=step, status="pending")
                    for step in steps
                },
                metadata=analysis_data
            )
            
            # Salvar estado no Redis
            await self._save_saga_state(analysis_id, saga_state)
            
            # Disparar primeiro passo
            await self._execute_step(analysis_id, steps[0], analysis_data)
            
            SAGA_ACTIVE.inc()
            
            logger.info(
                "saga_analysis_started",
                analysis_id=analysis_id,
                type=analysis_type,
                steps=steps
            )
            
            return analysis_id
            
        except Exception as e:
            SAGA_ERRORS.labels(
                step_name="start",
                error_type=type(e).__name__
            ).inc()
            
            logger.error(
                "saga_start_failed",
                analysis_id=analysis_id,
                error=str(e)
            )
            raise
    
    async def step_completed(self, analysis_id: str, step_name: str, result: Dict[str, Any]):
        """
        Marca um passo como completado e avança para o próximo
        
        Args:
            analysis_id: ID da análise
            step_name: Nome do passo completado
            result: Resultado do passo
        """
        try:
            # Carregar estado atual
            saga_state = await self._load_saga_state(analysis_id)
            if not saga_state:
                raise ValueError(f"SAGA {analysis_id} não encontrado")
            
            # Atualizar passo atual
            saga_state.steps[step_name].status = "completed"
            saga_state.steps[step_name].result = result
            saga_state.steps[step_name].completed_at = datetime.utcnow()
            
            SAGA_STEPS_TOTAL.labels(
                step_name=step_name,
                status="completed"
            ).inc()
            
            # Determinar próximo passo
            next_step = self._get_next_step(saga_state, step_name)
            
            if next_step:
                # Continuar SAGA
                saga_state.current_step = next_step
                await self._save_saga_state(analysis_id, saga_state)
                
                # Executar próximo passo
                await self._execute_step(analysis_id, next_step, saga_state.metadata)
                
                logger.info(
                    "saga_step_completed_continuing",
                    analysis_id=analysis_id,
                    completed_step=step_name,
                    next_step=next_step
                )
            else:
                # SAGA completado
                await self._complete_saga(analysis_id, saga_state)
                
                logger.info(
                    "saga_analysis_completed",
                    analysis_id=analysis_id,
                    final_step=step_name
                )
                
        except Exception as e:
            await self._handle_step_error(analysis_id, step_name, e)
    
    async def step_failed(self, analysis_id: str, step_name: str, error: str):
        """
        Marca um passo como falhado e inicia compensação
        
        Args:
            analysis_id: ID da análise
            step_name: Nome do passo que falhou
            error: Descrição do erro
        """
        try:
            saga_state = await self._load_saga_state(analysis_id)
            if not saga_state:
                raise ValueError(f"SAGA {analysis_id} não encontrado")
            
            # Marcar passo como falhado
            saga_state.steps[step_name].status = "failed"
            saga_state.steps[step_name].error = error
            saga_state.compensation_needed = True
            
            SAGA_STEPS_TOTAL.labels(
                step_name=step_name,
                status="failed"
            ).inc()
            
            SAGA_ERRORS.labels(
                step_name=step_name,
                error_type="step_failure"
            ).inc()
            
            # Iniciar compensação
            await self._start_compensation(analysis_id, saga_state)
            
            logger.error(
                "saga_step_failed",
                analysis_id=analysis_id,
                step_name=step_name,
                error=error
            )
            
        except Exception as e:
            logger.error(
                "saga_error_handling_failed",
                analysis_id=analysis_id,
                step_name=step_name,
                error=str(e)
            )
    
    def _get_analysis_steps(self, analysis_type: str) -> list:
        """Define passos baseado no tipo de análise"""
        
        base_steps = [
            "validation",           # Validação de dados
            "limits_check",        # Verificação de limites
            "risk_assessment",     # Avaliação de risco
            "mesa_consultation",   # Consulta ao MESA
            "decision_making",     # Tomada de decisão
            "notification"         # Notificação
        ]
        
        if analysis_type == "express":
            # Análise express pula alguns passos
            return [
                "validation",
                "limits_check",
                "mesa_consultation",
                "decision_making",
                "notification"
            ]
        elif analysis_type == "complex":
            # Análise complexa tem passos extras
            return [
                "validation",
                "enhanced_verification",
                "limits_check",
                "detailed_risk_assessment",
                "compliance_check",
                "mesa_consultation",
                "manual_review",
                "decision_making",
                "notification"
            ]
        else:
            # Análise padrão
            return base_steps
    
    async def _execute_step(self, analysis_id: str, step_name: str, data: Dict[str, Any]):
        """Executa um passo específico via Celery"""
        
        # Mapear passo para task Celery
        task_mapping = {
            "validation": "apps.saga.workflows.validate_request",
            "limits_check": "apps.saga.workflows.check_limits",
            "risk_assessment": "apps.saga.workflows.assess_risk",
            "mesa_consultation": "apps.saga.workflows.consult_mesa",
            "decision_making": "apps.saga.workflows.make_decision",
            "notification": "apps.saga.workflows.send_notification",
            "enhanced_verification": "apps.saga.workflows.enhanced_verification",
            "detailed_risk_assessment": "apps.saga.workflows.detailed_risk_assessment",
            "compliance_check": "apps.saga.workflows.compliance_check",
            "manual_review": "apps.saga.workflows.manual_review"
        }
        
        task_name = task_mapping.get(step_name)
        if not task_name:
            raise ValueError(f"Passo desconhecido: {step_name}")
        
        # Disparar tarefa assíncrona
        task = self.celery.send_task(
            task_name,
            args=[analysis_id, data],
            kwargs={"step_name": step_name},
            task_id=f"{analysis_id}_{step_name}",
            retry=True,
            retry_policy={
                'max_retries': 3,
                'interval_start': 1,
                'interval_step': 2,
                'interval_max': 10,
            }
        )
        
        logger.info(
            "saga_step_dispatched",
            analysis_id=analysis_id,
            step_name=step_name,
            task_id=task.id
        )
    
    async def _save_saga_state(self, analysis_id: str, saga_state: SAGAState):
        """Salva estado do SAGA no Redis"""
        key = f"saga:{analysis_id}"
        value = saga_state.model_dump_json()
        
        # TTL de 24 horas
        self.redis.setex(key, 86400, value)
    
    async def _load_saga_state(self, analysis_id: str) -> Optional[SAGAState]:
        """Carrega estado do SAGA do Redis"""
        key = f"saga:{analysis_id}"
        value = self.redis.get(key)
        
        if value:
            data = json.loads(value)
            return SAGAState(**data)
        return None
    
    def _get_next_step(self, saga_state: SAGAState, current_step: str) -> Optional[str]:
        """Determina próximo passo do SAGA"""
        steps = list(saga_state.steps.keys())
        try:
            current_index = steps.index(current_step)
            if current_index + 1 < len(steps):
                return steps[current_index + 1]
        except ValueError:
            pass
        return None
    
    async def _complete_saga(self, analysis_id: str, saga_state: SAGAState):
        """Completa o SAGA e atualiza banco"""
        from .database import db_transaction, log_audit_event
        from ..shared.models import CreditAnalysis, AnalysisStatus
        from datetime import datetime
        
        # Compilar resultado final
        final_result = {}
        for step_name, step in saga_state.steps.items():
            if step.result:
                final_result.update(step.result)
        
        # Atualizar banco
        with db_transaction() as db:
            analysis = db.query(CreditAnalysis).filter(
                CreditAnalysis.id == saga_state.analysis_id
            ).first()
            
            if analysis:
                analysis.status = final_result.get("status", AnalysisStatus.APPROVED.value)
                analysis.approved_amount = final_result.get("approved_amount")
                analysis.risk_score = final_result.get("risk_score")
                analysis.decision_reason = final_result.get("decision_reason")
                analysis.completed_at = datetime.utcnow()
                analysis.processing_time = int(
                    (analysis.completed_at - analysis.created_at).total_seconds() * 1000
                )
                
                # Log de auditoria
                log_audit_event(
                    db, analysis_id, "saga_completed", final_result, user_id="system"
                )
        
        # Limpar estado do Redis
        self.redis.delete(f"saga:{analysis_id}")
        SAGA_ACTIVE.dec()
    
    async def _start_compensation(self, analysis_id: str, saga_state: SAGAState):
        """Inicia processo de compensação (rollback)"""
        # Implementar rollback dos passos já executados
        # Por ora, apenas marca como erro e notifica
        
        from .database import db_transaction, log_audit_event
        from ..shared.models import CreditAnalysis, AnalysisStatus
        
        with db_transaction() as db:
            analysis = db.query(CreditAnalysis).filter(
                CreditAnalysis.id == saga_state.analysis_id
            ).first()
            
            if analysis:
                analysis.status = AnalysisStatus.ERROR.value
                analysis.decision_reason = "Erro interno no processamento"
                analysis.completed_at = datetime.utcnow()
                
                log_audit_event(
                    db, analysis_id, "saga_compensated", 
                    {"reason": "step_failure"}, user_id="system"
                )
        
        SAGA_ACTIVE.dec()
    
    async def _handle_step_error(self, analysis_id: str, step_name: str, error: Exception):
        """Trata erros gerais de passos"""
        SAGA_ERRORS.labels(
            step_name=step_name,
            error_type=type(error).__name__
        ).inc()
        
        await self.step_failed(analysis_id, step_name, str(error))


from datetime import datetime