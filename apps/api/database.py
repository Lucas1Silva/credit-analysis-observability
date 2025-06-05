"""
Configuração de banco de dados PostgreSQL
"""
import os
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.pool import QueuePool
import structlog

logger = structlog.get_logger()

# Configurações do banco
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://credit_user:credit_pass@postgres:5432/credit_analysis"
)

# Engine com pool de conexões otimizado para ambiente bancário
engine = create_engine(
    DATABASE_URL,
    poolclass=QueuePool,
    pool_size=20,          # Conexões simultâneas
    max_overflow=30,       # Conexões extras sob demanda
    pool_pre_ping=True,    # Verifica conexões antes de usar
    pool_recycle=3600,     # Recicla conexões a cada hora
    echo=False,            # Logs SQL (desabilitar em prod)
    connect_args={
        "options": "-c timezone=utc",
        "application_name": "credit-api",
        "connect_timeout": 10,
        "command_timeout": 30,
    }
)

# Session factory
SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine
)


# Event listeners para logging e métricas
@event.listens_for(engine, "before_cursor_execute")
def receive_before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
    """Log queries SQL para auditoria"""
    context._query_start_time = time.time()


@event.listens_for(engine, "after_cursor_execute")
def receive_after_cursor_execute(conn, cursor, statement, parameters, context, executemany):
    """Log duração das queries"""
    total = time.time() - context._query_start_time
    
    # Log apenas queries lentas (> 1s)
    if total > 1.0:
        logger.warning(
            "slow_query_detected",
            duration=total,
            statement=statement[:200] + "..." if len(statement) > 200 else statement
        )


def get_db() -> Session:
    """
    Dependency para obter sessão do banco
    Usado pelo FastAPI Depends()
    """
    db = SessionLocal()
    try:
        yield db
    except Exception as e:
        logger.error("database_error", error=str(e))
        db.rollback()
        raise
    finally:
        db.close()


def test_connection():
    """Testa conectividade com o banco"""
    try:
        db = SessionLocal()
        db.execute("SELECT 1")
        db.close()
        logger.info("database_connection_successful")
        return True
    except Exception as e:
        logger.error("database_connection_failed", error=str(e))
        return False


# === UTILITÁRIOS PARA TRANSAÇÕES ===

import time
from contextlib import contextmanager
from typing import Generator


@contextmanager
def db_transaction() -> Generator[Session, None, None]:
    """
    Context manager para transações manuais
    Útil para operações complexas do SAGA
    """
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception as e:
        logger.error("transaction_failed", error=str(e))
        db.rollback()
        raise
    finally:
        db.close()


def execute_with_retry(func, max_retries: int = 3, delay: float = 0.1):
    """
    Executa função com retry para deadlocks
    Essencial em ambiente bancário concorrente
    """
    for attempt in range(max_retries):
        try:
            return func()
        except Exception as e:
            if "deadlock" in str(e).lower() and attempt < max_retries - 1:
                logger.warning(
                    "deadlock_retry",
                    attempt=attempt + 1,
                    max_retries=max_retries
                )
                time.sleep(delay * (2 ** attempt))  # Exponential backoff
                continue
            raise


# === FUNÇÕES DE AUDITORIA ===

def log_audit_event(
    db: Session,
    analysis_id: str,
    event_type: str,
    event_data: dict,
    step_name: str = None,
    user_id: str = "system"
):
    """
    Registra evento de auditoria imutável
    Conforme compliance BACEN
    """
    from ..shared.models import AnalysisLog
    from uuid import UUID
    import json
    
    try:
        log_entry = AnalysisLog(
            analysis_id=UUID(analysis_id),
            event_type=event_type,
            step_name=step_name,
            event_data=event_data,
            user_id=user_id
        )
        
        db.add(log_entry)
        db.commit()
        
        logger.info(
            "audit_event_logged",
            analysis_id=analysis_id,
            event_type=event_type,
            step_name=step_name
        )
        
    except Exception as e:
        logger.error(
            "audit_logging_failed",
            analysis_id=analysis_id,
            event_type=event_type,
            error=str(e)
        )
        # Em ambiente bancário, falha de auditoria é crítica
        raise


# === MÉTRICAS DE BANCO ===

from prometheus_client import Histogram, Counter, Gauge

DB_QUERY_DURATION = Histogram(
    'database_query_duration_seconds',
    'Duração das queries do banco',
    ['operation', 'table']
)

DB_CONNECTIONS_ACTIVE = Gauge(
    'database_connections_active',
    'Conexões ativas no banco'
)

DB_ERRORS_TOTAL = Counter(
    'database_errors_total',
    'Total de erros do banco',
    ['error_type']
)


def update_db_metrics():
    """Atualiza métricas do banco para Prometheus"""
    try:
        # Conexões ativas
        active_connections = engine.pool.checkedout()
        DB_CONNECTIONS_ACTIVE.set(active_connections)
        
    except Exception as e:
        logger.error("db_metrics_update_failed", error=str(e))


# Inicialização
if __name__ == "__main__":
    # Teste de conexão
    if test_connection():
        print(" Conexão com PostgreSQL OK")
    else:
        print("Falha na conexão com PostgreSQL")
        exit(1)