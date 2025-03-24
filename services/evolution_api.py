# services/evolution_api.py
import requests
import logging
import time
import re

from typing import Dict, Any, List, Optional
from itertools import cycle
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from dotenv import load_dotenv
import os
import json

logger = logging.getLogger(__name__)

def create_retry_session(
    retries=3,
    backoff_factor=0.5,
    status_forcelist=(500, 502, 503, 504),
    allowed_methods=('GET', 'POST')
):
    """
    Cria uma sessão do requests com retry configurado
    """
    session = requests.Session()
    retry = Retry(
        total=retries,
        read=retries,
        connect=retries,
        backoff_factor=backoff_factor,
        status_forcelist=status_forcelist,
        allowed_methods=list(allowed_methods)
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    return session

def padronizar_telefone(numero: str) -> str:
    """
    Padroniza o número de telefone para o formato esperado pela Evolution API
    """
    # Remover caracteres não numéricos
    numero_limpo = re.sub(r'\D', '', numero)
    
    # Verificar se já tem código do país
    if not numero_limpo.startswith('55'):
        numero_limpo = '55' + numero_limpo
    
    # Garantir formato correto (com 9 para celulares)
    if len(numero_limpo) == 12:  # Sem o 9
        ddd = numero_limpo[2:4]
        numero_final = numero_limpo[4:]
        return f"{numero_limpo[:2]}{ddd}9{numero_final}"
    
    return numero_limpo

class EvolutionAPIService:
    _instance = None
    _current_instance_index = 0

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(EvolutionAPIService, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if not self._initialized:
            # Carrega variáveis de ambiente
            load_dotenv()
            
            # Obtém configurações básicas
            self.base_url = os.getenv('EVOLUTION_API_URL')
            self.timeout = int(os.getenv('EVOLUTION_API_TIMEOUT', '30'))
            self.max_retries = int(os.getenv('EVOLUTION_API_MAX_RETRIES', '3'))
            self.retry_delay = int(os.getenv('EVOLUTION_API_RETRY_DELAY', '2'))
            
            # Correção para o carregamento das instâncias
            try:
                instances_json = os.getenv("EVOLUTION_API_INSTANCES", "[]")
                # Verifique se o valor não está vazio e tem formato JSON válido
                if instances_json and instances_json.strip():
                    self.instances = json.loads(instances_json)
                else:
                    self.instances = []
                    logger.warning("Nenhuma instância da Evolution API configurada. Usando lista vazia.")
            except json.JSONDecodeError as e:
                logger.error(f"Erro ao decodificar JSON de instâncias: {e}")
                logger.error(f"Valor recebido: '{instances_json}'")
                self.instances = []
            
            if not self.instances:
                logger.warning("Nenhuma instância configurada. Adicione a configuração no arquivo .env")
            
            logger.info(f"Serviço Evolution API inicializado com {len(self.instances)} instâncias")
            
            self.session = create_retry_session(
                retries=self.max_retries,
                backoff_factor=self.retry_delay
            )
            self._initialized = True

    def _get_next_instance(self) -> Dict[str, str]:
        """
        Retorna a próxima instância no rodízio
        """
        instance = self.instances[self._current_instance_index]
        self.__class__._current_instance_index = (self._current_instance_index + 1) % len(self.instances)
        return instance

    def _get_headers(self, api_key: str) -> Dict[str, str]:
        return {
            'Content-Type': 'application/json',
            'apikey': api_key
        }

    def _make_request(self, method: str, url: str, headers: Dict, json_data: Dict = None) -> Dict[str, Any]:
        """
        Faz uma requisição com retry e tratamento de erros
        """
        for attempt in range(self.max_retries):
            try:
                response = self.session.request(
                    method=method,
                    url=url,
                    headers=headers,
                    json=json_data,
                    timeout=self.timeout
                )
                
                if response.status_code == 200 or 'PENDING' in response.text.upper():
                    return {
                        'status': 'success',
                        'data': response.json() if response.content else {}
                    }
                
                logger.error(f"Erro na requisição (tentativa {attempt + 1}): Status {response.status_code}, Response: {response.text}")
                
                if attempt < self.max_retries - 1:
                    time.sleep(self.retry_delay * (attempt + 1))
                    continue
                
                return {
                    'status': 'error',
                    'message': f"Erro na requisição: {response.text}"
                }

            except requests.Timeout:
                logger.error(f"Timeout na requisição (tentativa {attempt + 1})")
                if attempt < self.max_retries - 1:
                    time.sleep(self.retry_delay * (attempt + 1))
                    continue
                return {
                    'status': 'error',
                    'message': 'Timeout na requisição'
                }

            except Exception as e:
                logger.error(f"Erro na requisição (tentativa {attempt + 1}): {str(e)}")
                if attempt < self.max_retries - 1:
                    time.sleep(self.retry_delay * (attempt + 1))
                    continue
                return {
                    'status': 'error',
                    'message': str(e)
                }

        return {
            'status': 'error',
            'message': 'Todas as tentativas falharam'
        }

    def send_text_message(self, numero: str, mensagem: str, instance_data: Dict = None) -> Dict[str, Any]:
        """
        Envia uma mensagem de texto simples usando a instância fornecida
        """
        try:
            numero_padronizado = padronizar_telefone(numero)
            
            if instance_data is None:
                instance_data = self._get_next_instance()

            instance = instance_data['instance']
            api_key = instance_data['api_key']

            url = f"{self.base_url}/message/sendText/{instance}"
            
            # Formato correto conforme documentação da Evolution API
            payload = {
                "number": numero_padronizado,
                "text": mensagem,  # Usando o campo 'text' em vez de aninhado em textMessage
                "options": {
                    "delay": 1200,
                    "presence": "composing"
                }
            }

            logger.info(f"Enviando mensagem via instância {instance} para {numero_padronizado}")
            logger.debug(f"Payload da requisição: {json.dumps(payload)}")
            
            response = self._make_request(
                method='POST',
                url=url,
                headers=self._get_headers(api_key),
                json_data=payload
            )
            
            if response['status'] == 'success':
                response['instance'] = instance
            
            return response

        except Exception as e:
            logger.error(f"Erro ao enviar mensagem de texto: {str(e)}")
            return {
                'status': 'error',
                'message': str(e)
            }

    def send_list_message(self, numero: str, titulo: str, descricao: str, alternativas: List[Dict[str, str]], instance_data: Dict = None) -> Dict[str, Any]:
        """
        Envia uma mensagem com lista de opções usando a instância fornecida
        """
        try:
            numero_padronizado = padronizar_telefone(numero)

            if not alternativas:
                return self.send_text_message(numero_padronizado, descricao, instance_data)

            if instance_data is None:
                instance_data = self._get_next_instance()

            instance = instance_data['instance']
            api_key = instance_data['api_key']

            url = f"{self.base_url}/message/sendList/{instance}"
            
            payload = {
                "number": numero_padronizado,
                "options": {
                    "delay": 1200,
                    "presence": "composing"
                },
                "listMessage": {
                    "title": titulo,
                    "description": descricao,
                    "buttonText": "Escolher",
                    "footerText": "Responda selecionando uma opção abaixo",
                    "sections": [
                        {
                            "title": "Alternativas",
                            "rows": [
                                {
                                    "title": alt['texto'],
                                    "rowId": str(idx + 1)
                                } for idx, alt in enumerate(alternativas)
                            ]
                        }
                    ]
                }
            }

            logger.info(f"Enviando mensagem com lista via instância {instance} para {numero_padronizado}")
            
            response = self._make_request(
                method='POST',
                url=url,
                headers=self._get_headers(api_key),
                json_data=payload
            )
            
            if response['status'] == 'success':
                response['instance'] = instance
            
            return response

        except Exception as e:
            logger.error(f"Erro ao enviar mensagem com lista: {str(e)}")
            return {
                'status': 'error',
                'message': str(e)
            }

    def verify_whatsapp_numbers(self, numeros: List[str], instance_data: Dict = None) -> Dict[str, Any]:
        """
        Verifica se os números fornecidos são números válidos do WhatsApp.
        
        Args:
            numeros: Lista de números para verificar
            instance_data: Dados da instância específica a ser usada (opcional)
            
        Returns:
            Dict com status da requisição e dados dos números verificados
        """
        try:
            numeros_padronizados = [padronizar_telefone(num) for num in numeros]
            
            if instance_data is None:
                instance_data = self._get_next_instance()

            instance = instance_data['instance']
            api_key = instance_data['api_key']

            url = f"{self.base_url}/chat/whatsappNumbers/{instance}"
            
            payload = {
                "numbers": numeros_padronizados
            }

            logger.info(f"Verificando números via instância {instance}: {numeros_padronizados}")
            
            response = self._make_request(
                method='POST',
                url=url,
                headers=self._get_headers(api_key),
                json_data=payload
            )
            
            if response['status'] == 'success':
                response['instance'] = instance
            
            return response

        except Exception as e:
            logger.error(f"Erro ao verificar números: {str(e)}")
            return {
                'status': 'error',
                'message': str(e)
            }