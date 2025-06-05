# Multi-stage build para aplicações Python
FROM python:3.11-slim as base

# Configurações do sistema
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Instalar dependências do sistema
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    libpq-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Criar usuário não-root
RUN useradd --create-home --shell /bin/bash credit

# Stage de dependências
FROM base as dependencies

WORKDIR /app

# Instalar dependências Python
COPY requirements.txt .
RUN pip install --upgrade pip && \
    pip install -r requirements.txt

# Stage final
FROM dependencies as final

# Copiar código da aplicação
COPY apps/ /app/apps/

# Criar diretórios necessários
RUN mkdir -p /app/logs && \
    chown -R credit:credit /app

# Mudar para usuário não-root
USER credit

# Configurar PATH
ENV PATH="/home/credit/.local/bin:$PATH"

# Healthcheck padrão
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Porta padrão
EXPOSE 8000

# Comando padrão (será sobrescrito pelo docker-compose)
CMD ["uvicorn", "apps.api.main:app", "--host", "0.0.0.0", "--port", "8000"]