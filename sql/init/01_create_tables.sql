-- Extensões necessárias
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pg_stat_statements";

-- Schema para auditoria imutável
CREATE SCHEMA IF NOT EXISTS audit;
CREATE SCHEMA IF NOT EXISTS metrics;

-- Tabela de empresas (dados básicos)
CREATE TABLE companies (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    cnpj VARCHAR(18) UNIQUE NOT NULL,
    name VARCHAR(255) NOT NULL,
    segment VARCHAR(100),
    annual_revenue BIGINT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Tabela de análises de crédito (SAGA state)
CREATE TABLE credit_analyses (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    company_id UUID REFERENCES companies(id),
    requested_amount BIGINT NOT NULL,
    analysis_type VARCHAR(50) NOT NULL, -- 'standard', 'express', 'complex'
    status VARCHAR(50) NOT NULL DEFAULT 'pending', -- 'pending', 'processing', 'approved', 'rejected', 'error'
    current_step VARCHAR(100),
    saga_state JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP,
    decision_reason TEXT,
    approved_amount BIGINT,
    risk_score INTEGER CHECK (risk_score >= 0 AND risk_score <= 1000)
);

-- Tabela de logs imutáveis (compliance BACEN) - PARTICIONADA
CREATE TABLE audit.analysis_logs (
    id UUID DEFAULT uuid_generate_v4(),
    analysis_id UUID REFERENCES credit_analyses(id),
    event_type VARCHAR(50) NOT NULL, -- 'step_start', 'step_complete', 'decision', 'error'
    step_name VARCHAR(100),
    event_data JSONB NOT NULL,
    user_id VARCHAR(100), -- quem executou
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    hash_previous VARCHAR(64), -- para cadeia imutável
    hash_current VARCHAR(64), -- SHA256 do registro atual
    PRIMARY KEY (id, timestamp)
) PARTITION BY RANGE (timestamp);

-- Tabela de métricas de negócio (agregações)
CREATE TABLE metrics.business_metrics (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    metric_name VARCHAR(100) NOT NULL,
    metric_value DECIMAL(15,2),
    metric_unit VARCHAR(20), -- 'BRL', 'percentage', 'count', 'seconds'
    dimensions JSONB, -- {"segment": "tech", "amount_range": "1M-10M"}
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    date_partition DATE GENERATED ALWAYS AS (DATE(timestamp)) STORED
);

-- Tabela de limites por holding/subsidiária
CREATE TABLE company_limits (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    company_id UUID REFERENCES companies(id),
    parent_company_id UUID REFERENCES companies(id), -- para hierarquia
    limit_type VARCHAR(50) NOT NULL, -- 'individual', 'consolidated'
    total_limit BIGINT NOT NULL,
    used_limit BIGINT DEFAULT 0,
    available_limit BIGINT GENERATED ALWAYS AS (total_limit - used_limit) STORED,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT positive_limits CHECK (total_limit > 0 AND used_limit >= 0)
);

-- Índices para performance
CREATE INDEX idx_analyses_company_created ON credit_analyses(company_id, created_at DESC);
CREATE INDEX idx_analyses_status_created ON credit_analyses(status, created_at DESC);
CREATE INDEX idx_audit_logs_analysis_timestamp ON audit.analysis_logs(analysis_id, timestamp DESC);
CREATE INDEX idx_business_metrics_name_date ON metrics.business_metrics(metric_name, date_partition DESC);
CREATE INDEX idx_company_limits_company ON company_limits(company_id);

-- Particionamento para logs (por mês)
CREATE TABLE audit.analysis_logs_y2025m01 PARTITION OF audit.analysis_logs
FOR VALUES FROM ('2025-01-01') TO ('2025-02-01');
CREATE TABLE audit.analysis_logs_y2025m02 PARTITION OF audit.analysis_logs
FOR VALUES FROM ('2025-02-01') TO ('2025-03-01');
CREATE TABLE audit.analysis_logs_y2025m03 PARTITION OF audit.analysis_logs
FOR VALUES FROM ('2025-03-01') TO ('2025-04-01');

-- Função para calcular hash da cadeia imutável
CREATE OR REPLACE FUNCTION calculate_log_hash(
    p_analysis_id UUID,
    p_event_type VARCHAR,
    p_event_data JSONB,
    p_previous_hash VARCHAR
) RETURNS VARCHAR AS $$
BEGIN
    RETURN encode(
        sha256(
            (p_analysis_id::text || p_event_type || p_event_data::text || COALESCE(p_previous_hash, ''))::bytea
        ), 
        'hex'
    );
END;
$$ LANGUAGE plpgsql;

-- Trigger para hash automático
CREATE OR REPLACE FUNCTION set_log_hash() RETURNS TRIGGER AS $$
DECLARE
    prev_hash VARCHAR(64);
BEGIN
    -- Busca o hash do último registro da análise
    SELECT hash_current INTO prev_hash
    FROM audit.analysis_logs
    WHERE analysis_id = NEW.analysis_id
    ORDER BY timestamp DESC
    LIMIT 1;
    
    NEW.hash_previous := prev_hash;
    NEW.hash_current := calculate_log_hash(
        NEW.analysis_id,
        NEW.event_type,
        NEW.event_data,
        prev_hash
    );
    
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trigger_set_log_hash
    BEFORE INSERT ON audit.analysis_logs
    FOR EACH ROW EXECUTE FUNCTION set_log_hash();

-- Dados de exemplo para teste
INSERT INTO companies (cnpj, name, segment, annual_revenue) VALUES
('12.345.678/0001-90', 'TechCorp Ltda', 'Technology', 50000000),
('98.765.432/0001-10', 'Logística S.A.', 'Logistics', 120000000),
('11.222.333/0001-44', 'Indústria XYZ', 'Manufacturing', 300000000);

INSERT INTO company_limits (company_id, limit_type, total_limit) 
SELECT id, 'individual', 
    CASE 
        WHEN annual_revenue < 100000000 THEN 10000000
        WHEN annual_revenue < 200000000 THEN 25000000
        ELSE 50000000
    END
FROM companies;