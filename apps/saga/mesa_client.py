"""
Cliente MESA - Simulador do sistema de decisão via Kafka
Simula comportamento real do MESA para a POC
"""
import os
import json
import asyncio
import random
from datetime import datetime
from typing import Dict, Any
from uuid import UUID

import structlog
from kafka import KafkaProducer, KafkaConsumer
from kafka.errors import KafkaError

from ..shared.models import MESARequest, MESAResponse

logger = structlog.get_logger()

KAFKA_BOOTSTRAP_SERVERS = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "kafka:29092")


class MESAClient:
    """Cliente para comunicação com o sistema MESA via Kafka"""
    
    def __init__(self):
        self.bootstrap_servers = KAFKA_BOOTSTRAP_SERVERS
        self.request_topic = "mesa-requests"
        self.response_topic = "mesa-responses"
        
        # Producer para enviar requisições
        self.producer = KafkaProducer(
            bootstrap_servers=self.bootstrap_servers,
            value_serializer=lambda v: json.dumps(v).encode('utf-8'),
            key_serializer=lambda k: k.encode('utf-8') if k else None,
            acks='all',  # Garantir entrega
            retries=3,
            batch_size=16384,
            linger_ms=10,
            max_request_size=1048576,
            compression_type='gzip'
        )
        
        # Consumer para receber respostas
        self.consumer = KafkaConsumer(
            self.response_topic,
            bootstrap_servers=self.bootstrap_servers,
            value_deserializer=lambda v: json.loads(v.decode('utf-8')),
            key_deserializer=lambda k: k.decode('utf-8') if k else None,
            group_id='credit-api-consumers',
            auto_offset_reset='latest',
            enable_auto_commit=True,
            consumer_timeout_ms=30000  # 30s timeout
        )
        
        logger.info("mesa_client_initialized", 
                   servers=self.bootstrap_servers,
                   request_topic=self.request_topic,
                   response_topic=self.response_topic)
    
    async def send_request(self, request: MESARequest) -> MESAResponse:
        """
        Envia requisição para MESA e aguarda resposta
        
        Args:
            request: Dados da requisição
            
        Returns:
            MESAResponse: Resposta do MESA
        """
        try:
            # Serializar requisição
            request_data = request.model_dump()
            request_data['analysis_id'] = str(request_data['analysis_id'])
            request_data['timestamp'] = request_data['timestamp'].isoformat()
            
            # Enviar via Kafka
            key = str(request.analysis_id)
            future = self.producer.send(
                self.request_topic,
                key=key,
                value=request_data
            )
            
            # Aguardar confirmação de envio
            record_metadata = future.get(timeout=10)
            
            logger.info("mesa_request_sent",
                       analysis_id=str(request.analysis_id),
                       topic=record_metadata.topic,
                       partition=record_metadata.partition,
                       offset=record_metadata.offset)
            
            # Aguardar resposta
            response = await self._wait_for_response(str(request.analysis_id))
            
            return response
            
        except KafkaError as e:
            logger.error("mesa_kafka_error", 
                        analysis_id=str(request.analysis_id),
                        error=str(e))
            raise
        except Exception as e:
            logger.error("mesa_request_failed",
                        analysis_id=str(request.analysis_id),
                        error=str(e))
            raise
    
    async def _wait_for_response(self, analysis_id: str, timeout: int = 30) -> MESAResponse:
        """
        Aguarda resposta específica do MESA
        Em ambiente real, usaria correlation ID
        """
        start_time = datetime.utcnow()
        
        # Por ser POC, vamos simular resposta diretamente
        # Em produção, ficaria escutando o Kafka
        await asyncio.sleep(random.uniform(1.5, 3.0))  # Simular latência MESA
        
        # Simular decisão do MESA
        response = self._simulate_mesa_decision(analysis_id)
        
        logger.info("mesa_response_received",
                   analysis_id=analysis_id,
                   decision=response.decision,
                   processing_time=response.processing_time)
        
        return response
    
    def _simulate_mesa_decision(self, analysis_id: str) -> MESAResponse:
        """
        Simula decisão do MESA baseada em regras
        Em produção, seria a resposta real do sistema
        """
        
        # Simular variabilidade nas decisões
        risk_score = random.randint(100, 900)
        confidence = random.uniform(0.7, 0.95)
        processing_time = random.uniform(1.0, 3.5)
        
        # Regras de decisão simuladas
        if risk_score > 800:
            decision = "rejected"
            approved_amount = None
        elif risk_score > 600:
            if random.random() > 0.3:  # 70% chance de aprovação
                decision = "approved"
                # Reduzir valor aprovado para riscos médios
                base_amount = random.randint(1000000, 10000000)
                approved_amount = int(base_amount * random.uniform(0.6, 0.9))
            else:
                decision = "manual_review"
                approved_amount = None
        else:
            decision = "approved"
            # Valor total ou quase total para baixo risco
            base_amount = random.randint(1000000, 50000000)
            approved_amount = int(base_amount * random.uniform(0.9, 1.0))
        
        # Fatores de risco simulados
        risk_factors = {
            "credit_bureau_score": random.randint(300, 850),
            "payment_history": random.choice(["excellent", "good", "fair", "poor"]),
            "debt_to_income": random.uniform(0.1, 0.8),
            "industry_outlook": random.choice(["positive", "stable", "negative"]),
            "economic_indicators": random.choice(["favorable", "neutral", "concerning"]),
            "collateral_value": random.randint(0, 100000000) if decision == "approved" else 0
        }
        
        return MESAResponse(
            analysis_id=UUID(analysis_id),
            decision=decision,
            approved_amount=approved_amount,
            risk_score=risk_score,
            risk_factors=risk_factors,
            confidence=confidence,
            processing_time=processing_time,
            timestamp=datetime.utcnow()
        )
    
    def close(self):
        """Fecha conexões Kafka"""
        try:
            self.producer.close()
            self.consumer.close()
            logger.info("mesa_client_closed")
        except Exception as e:
            logger.error("mesa_client_close_error", error=str(e))


# === MESA SIMULATOR (Worker separado) ===

class MESASimulator:
    """
    Simulador que roda como worker separado
    Consome requisições e produz respostas
    """
    
    def __init__(self):
        self.bootstrap_servers = KAFKA_BOOTSTRAP_SERVERS
        self.request_topic = "mesa-requests"
        self.response_topic = "mesa-responses"
        
        # Consumer para requisições
        self.consumer = KafkaConsumer(
            self.request_topic,
            bootstrap_servers=self.bootstrap_servers,
            value_deserializer=lambda v: json.loads(v.decode('utf-8')),
            key_deserializer=lambda k: k.decode('utf-8') if k else None,
            group_id='mesa-simulator',
            auto_offset_reset='earliest',
            enable_auto_commit=True
        )
        
        # Producer para respostas
        self.producer = KafkaProducer(
            bootstrap_servers=self.bootstrap_servers,
            value_serializer=lambda v: json.dumps(v).encode('utf-8'),
            key_serializer=lambda k: k.encode('utf-8') if k else None,
            acks='all'
        )
        
        logger.info("mesa_simulator_initialized")
    
    def run(self):
        """Loop principal do simulador"""
        logger.info("mesa_simulator_started")
        
        try:
            for message in self.consumer:
                try:
                    # Processar requisição
                    request_data = message.value
                    analysis_id = request_data.get('analysis_id')
                    
                    logger.info("mesa_processing_request", analysis_id=analysis_id)
                    
                    # Simular tempo de processamento
                    import time
                    processing_time = random.uniform(1.0, 4.0)
                    time.sleep(processing_time)
                    
                    # Gerar resposta
                    response = self._generate_response(request_data, processing_time)
                    
                    # Enviar resposta
                    self.producer.send(
                        self.response_topic,
                        key=analysis_id,
                        value=response
                    )
                    
                    logger.info("mesa_response_sent", 
                               analysis_id=analysis_id,
                               decision=response['decision'])
                    
                except Exception as e:
                    logger.error("mesa_processing_error", 
                                analysis_id=request_data.get('analysis_id'),
                                error=str(e))
                    
        except KeyboardInterrupt:
            logger.info("mesa_simulator_stopping")
        finally:
            self.consumer.close()
            self.producer.close()
    
    def _generate_response(self, request_data: Dict[str, Any], processing_time: float) -> Dict[str, Any]:
        """Gera resposta baseada na requisição"""
        
        analysis_id = request_data['analysis_id']
        requested_amount = request_data['requested_amount']
        company_data = request_data.get('company_data', {})
        risk_factors = request_data.get('risk_factors', {})
        
        # Lógica de decisão mais sofisticada
        segment = company_data.get('segment', 'unknown')
        utilization = company_data.get('utilization_percent', 0)
        
        # Score base por segmento
        segment_scores = {
            'Technology': random.randint(200, 600),
            'Manufacturing': random.randint(300, 700),
            'Logistics': random.randint(400, 800),
            'Financial': random.randint(250, 550),
            'unknown': random.randint(500, 900)
        }
        
        base_score = segment_scores.get(segment, 500)
        
        # Ajustes
        if utilization > 80:
            base_score += 200
        elif utilization > 50:
            base_score += 100
        
        if requested_amount > 50000000:  # > R$ 500k
            base_score += 150
        
        risk_score = min(1000, max(0, base_score + random.randint(-100, 100)))
        
        # Decisão final
        if risk_score > 850:
            decision = "rejected"
            approved_amount = None
        elif risk_score > 650:
            if random.random() > 0.4:
                decision = "approved" 
                approved_amount = int(requested_amount * random.uniform(0.5, 0.8))
            else:
                decision = "manual_review"
                approved_amount = None
        else:
            decision = "approved"
            approved_amount = int(requested_amount * random.uniform(0.9, 1.0))
        
        return {
            'analysis_id': analysis_id,
            'decision': decision,
            'approved_amount': approved_amount,
            'risk_score': risk_score,
            'risk_factors': {
                'credit_score': random.randint(300, 850),
                'financial_health': random.choice(['excellent', 'good', 'fair', 'poor']),
                'market_conditions': random.choice(['favorable', 'neutral', 'challenging']),
                'regulatory_compliance': random.choice([True, False]),
                'collateral_assessment': random.uniform(0.5, 1.5) if decision == 'approved' else 0.0
            },
            'confidence': random.uniform(0.7, 0.95),
            'processing_time': processing_time,
            'timestamp': datetime.utcnow().isoformat()
        }


# === SCRIPT PARA EXECUTAR SIMULADOR ===

def run_mesa_simulator():
    """Função para executar o simulador em processo separado"""
    simulator = MESASimulator()
    simulator.run()


if __name__ == "__main__":
    run_mesa_simulator()