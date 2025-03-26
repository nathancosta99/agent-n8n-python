from fastapi import FastAPI, Request, HTTPException
import requests
import json
import re
import os
import logging
from dotenv import load_dotenv
from pydantic import BaseModel
from supabase import create_client, Client
from openai import OpenAI
from datetime import datetime
from typing import List, Dict, Optional, Any
from services.evolution_api import EvolutionAPIService, padronizar_telefone
from message_processor import MessageProcessor

# 🔹 Carregar variáveis de ambiente
load_dotenv()

# 🔹 Configurar Logs
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Instanciar MessageProcessor
message_processor = MessageProcessor()
logger.info("✅ MessageProcessor inicializado com sucesso")

# Inicialize o serviço
try:
    evolution_service = EvolutionAPIService()
    logger.info("✅ Serviço Evolution API inicializado com sucesso")
except Exception as e:
    logger.error(f"❌ Erro ao inicializar serviço Evolution API: {str(e)}")
    # Fallback para métodos antigos
    evolution_service = None



# 🔹 Configurar Supabase
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Adicione estes logs para debug
logger.info(f"URL Supabase: {SUPABASE_URL}")
logger.info(f"Supabase Key: {SUPABASE_KEY[:5]}...") 

# 🔹 Configurar OpenAI
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# 🔹 Configurar FastAPI
app = FastAPI()


# 🔹 Modelo de dados para entrada do Webhook
class MessageData(BaseModel):
    key: dict
    message: dict
    text: str = None

# 🔹 Classe para gerenciar o histórico de conversas
class ChatMemory:
    def __init__(self, supabase_client, table_name="chat_memory"):
        self.supabase = supabase_client
        self.table_name = table_name
        self._ensure_table_exists()
    
    def _ensure_table_exists(self):
        """Verifica se a tabela de memória existe e cria se necessário"""
        try:
            # Esta função é apenas para verificar se conseguimos acessar a tabela
            # Na implementação real com Supabase, você precisaria criar a tabela
            # manualmente ou através de migrações
            self.supabase.table(self.table_name).select("*").limit(1).execute()
            logger.info(f"✅ Tabela {self.table_name} acessada com sucesso")
        except Exception as e:
            logger.error(f"Erro ao acessar tabela {self.table_name}: {str(e)}")
            # Aqui você poderia implementar a criação da tabela se for necessário
    
    async def get_conversation_history(self, session_id: str, max_messages: int = 20) -> List[Dict[str, Any]]:
        """
        Recupera o histórico de conversas para um determinado session_id
        """
        try:
            query = self.supabase.table(self.table_name) \
                .select("*") \
                .eq("session_id", session_id) \
                .order("timestamp", desc=True) \
                .limit(max_messages) \
                .execute()
            
            # Inverte a ordem para que as mensagens mais antigas venham primeiro
            messages = reversed(query.data) if query.data else []
            return list(messages)
        except Exception as e:
            logger.error(f"Erro ao recuperar histórico de chat para {session_id}: {str(e)}")
            return []
    
    async def add_message(self, session_id: str, role: str, content: str) -> bool:
        """
        Adiciona uma nova mensagem ao histórico de conversas
        """
        try:
            message_data = {
                "session_id": session_id,
                "role": role,
                "content": content,
                "timestamp": datetime.now().isoformat()
            }
            
            self.supabase.table(self.table_name).insert(message_data).execute()
            logger.info(f"✅ Mensagem adicionada ao histórico para {session_id}")
            return True
        except Exception as e:
            logger.error(f"Erro ao adicionar mensagem ao histórico para {session_id}: {str(e)}")
            return False
    
    async def format_messages_for_openai(self, session_id: str, max_messages: int = 10) -> List[Dict[str, str]]:
        """
        Formata o histórico de mensagens para o formato esperado pela API do OpenAI
        """
        history = await self.get_conversation_history(session_id, max_messages)
        formatted_messages = []
        
        for msg in history:
            formatted_messages.append({
                "role": msg["role"],
                "content": msg["content"]
            })
            
        return formatted_messages

# 📌 Função para verificar cobertura e planos disponíveis
def verificar_cobertura(cidade, bairro=None, zona=None):
    try:
        # Verificar se a cidade foi encontrada antes de continuar
        if not cidade:
            logger.warning("Cidade não especificada na mensagem")
            return False, None
            
        # Lógica específica baseada na cidade
        if cidade.lower() not in ["teresina", "guadalupe"]:
            logger.info(f"Cidade {cidade} não é atendida pela SMNET")
            return False, None
            
        if cidade.lower() == "teresina":
            # Precisamos do bairro para Teresina
            if not bairro:
                logger.info("Bairro não especificado para Teresina")
                return None, None  # Retorno especial para indicar que precisamos do bairro
                
            # Consultar a tabela cliente_cadastro para verificar cobertura no bairro
            query = supabase.table("cliente_cadastro").select("*").eq("cidade", cidade).eq("bairro", bairro).execute()
            
            # Se não há clientes nesse bairro, provavelmente não há cobertura
            if len(query.data) == 0:
                logger.info(f"Nenhum cliente encontrado no bairro {bairro} em Teresina. Possível falta de cobertura.")
                return False, None
                
        elif cidade.lower() == "guadalupe":
            # Verificar zona para Guadalupe
            if zona and zona.lower() == "rural":
                logger.info("Zona rural de Guadalupe não possui cobertura")
                return False, None
                
        # Buscar planos disponíveis - poderia ser de outra tabela ou fixo
        planos = {
            "100MB": "R$ 99,90",
            "200MB": "R$ 129,90",
            "300MB": "R$ 149,90",
            "500MB": "R$ 199,90"
        }
        
        return True, planos
    except Exception as e:
        logger.error(f"Erro ao verificar cobertura usando cliente_cadastro: {str(e)}")
        # Em caso de erro, retornar dados de teste
        if cidade and cidade.lower() in ["teresina", "guadalupe"]:
            planos = {
                "100MB": "R$ 99,90",
                "200MB": "R$ 129,90",
                "300MB": "R$ 149,90"
            }
            return True, planos
        return False, None

# 📌 Função para gerar resposta com base nos prompts do n8n
async def generate_ai_response(text, user_data=None, session_id=None):
    try:
        # Instanciar o chat memory
        chat_memory = ChatMemory(supabase)
        
        # Prompt completo da Julia
        sistema_prompt = """
Apresentação Inicial:
"Olá, eu sou a Julia, consultora da SMNET! Se você está interessado em contratar a internet mais rápida da região, me diga para qual cidade deseja contratar para que possamos continuar o atendimento."

Função:
Você é Julia, consultora de atendimento da SMNET. Seu objetivo é verificar a cobertura de internet, apresentar os planos disponíveis e coletar os dados do cliente para encaminhá-lo ao setor responsável pela instalação.

Você sempre deve iniciar a conversa com a Apresentação Inicial no primeiro contato, seguindo o modelo:
"Olá, eu sou a Julia, consultora da SMNET! Se você está interessado em contratar a internet mais rápida da região, me diga para qual cidade deseja contratar para que possamos continuar o atendimento."

Seu atendimento deve ser eficiente, amigável, humanizado e direto ao ponto, garantindo uma experiência fluida e sem distrações. Você foca exclusivamente na venda dos planos da SMNET e não permite desvios de assunto.

Tarefa:
Atender clientes de forma rápida, clara e objetiva, guiando-os no processo de verificação de cobertura, escolha do plano e coleta dos dados necessários para a instalação.

Se a região for atendida, apresente os planos disponíveis corretamente, colete todos os dados necessários e encaminhe a solicitação para o setor responsável pela instalação.

Se a região não for atendida, finalize a conversa com empatia e profissionalismo, informando que a SMNET pode entrar em contato no futuro.

Contexto:
A SMNET oferece planos de internet APENAS para Teresina e Guadalupe.

Em Teresina, a cobertura varia por bairro ou localidade, então é essencial coletar essa informação antes de apresentar os planos.
Em Guadalupe, basta saber se o cliente deseja contratar para zona urbana ou zona rural, pois a cobertura só está disponível na zona urbana.
"""

        # Adicionar informações sobre cobertura e planos disponíveis se aplicável
        if user_data and "cidade" in user_data:
            sistema_prompt += f"\nO cliente está interessado na cidade: {user_data['cidade']}"
            
            if "bairro" in user_data:
                sistema_prompt += f"\nBairro/localidade informado: {user_data['bairro']}"
                
            if "cobertura" in user_data:
                if user_data["cobertura"]:
                    sistema_prompt += "\nESTA REGIÃO POSSUI COBERTURA!"
                    if "planos" in user_data and user_data["planos"]:
                        sistema_prompt += "\nPlanos disponíveis para esta região:"
                        for plano, valor in user_data["planos"].items():
                            sistema_prompt += f"\n- {plano}: {valor}"
                else:
                    sistema_prompt += "\nESTA REGIÃO NÃO POSSUI COBERTURA!"
        
        # Prepara a lista de mensagens para o OpenAI
        messages = [{"role": "system", "content": sistema_prompt}]
        
        # Recuperar histórico de conversas se houver um session_id
        if session_id:
            conversation_history = await chat_memory.format_messages_for_openai(session_id)
            # Adicionar histórico somente se houver mensagens
            if conversation_history:
                messages.extend(conversation_history)
        
        # Adicionar a mensagem atual do usuário
        messages.append({"role": "user", "content": text})
        
        # Usando a nova API do OpenAI
        response = client.chat.completions.create(
            model="gpt-4",
            messages=messages
        )
        
        ai_response = response.choices[0].message.content
        
        # Salvar a interação no histórico se houver um session_id
        if session_id:
            await chat_memory.add_message(session_id, "user", text)
            await chat_memory.add_message(session_id, "assistant", ai_response)
        
        return ai_response
    except Exception as e:
        logger.error(f"Erro ao chamar OpenAI: {str(e)}")
        return "Não consegui processar sua solicitação."

def is_cadastro_completo(user_data: Dict[str, Any]) -> bool:
    campos_obrigatorios = ["nome", "cpf", "telefone", "cidade", "bairro", "plano_escolhido"]
    return all(user_data.get(campo) for campo in campos_obrigatorios)

# 📌 Função para salvar cadastro do cliente
def save_client_data(data):
    try:
        if not is_cadastro_completo(data):
            logger.warning("❌ Tentativa de salvar cadastro incompleto. A operação foi ignorada.")
            return False

        cliente_data = {
            "telefone": data["telefone"],
            "nome": data.get("nome", ""),
            "cpf": data.get("cpf", ""),
            "data_nascimento": data.get("data_nascimento", ""),
            "email": data.get("email", ""),
            "cidade": data.get("cidade", ""),
            "bairro": data.get("bairro", ""),
            "endereco": data.get("endereco", ""),
            "plano_escolhido": data.get("plano_escolhido", ""),
            "status": "pendente_instalacao"
        }

        supabase.table("cliente_cadastro").upsert(cliente_data).execute()
        logger.info(f"✅ Cadastro do cliente salvo com sucesso: {data['telefone']}")
        return True
    except Exception as e:
        logger.error(f"Erro ao salvar cadastro do cliente: {str(e)}")
        return False


# 📌 Webhook para receber mensagens
@app.post("/webhook")
async def receive_message(request: Request):
    try:
        # Obter dados brutos da requisição
        data = await request.json()
        
        # Logar a estrutura completa para diagnóstico
        logger.info(f"Dados recebidos no webhook: {json.dumps(data, indent=2)}")
        
        if "message" in data:
            messages = data["message"]
            if isinstance(messages, list) and len(messages) > 0:
                logger.debug(f"📄 Formato de lista de mensagens detectado com {len(messages)} mensagens")
                message_data = messages[0]
                sender = message_data.get("key", {}).get("remoteJid", "")
                message_obj = message_data.get("message", {})
                
                logger.debug(f"📄 Estrutura da chave: {json.dumps(message_data.get('key', {}), default=str)}")
                logger.debug(f"📄 Tipo do objeto message: {type(message_obj)}, Tem dados: {bool(message_obj)}")
                
                # Extrair texto da mensagem ou transcrever áudio
                text = ""
                if "audioMessage" in message_obj:
                    logger.debug("🎤 Mensagem de áudio detectada, iniciando transcrição")
                    audio_data = message_obj["audioMessage"]
                    
                    # Verificar se existe uma representação base64 no webhook
                    if "base64" in message_obj:
                        logger.info("🎤 Usando representação base64 direta do webhook")
                        audio_data["base64"] = message_obj["base64"]
                    
                    # Adicionar informações completas para debug
                    logger.debug(f"🎤 Estrutura completa do audioMessage: {json.dumps(audio_data, default=str)[:500]}...")
                    
                    text = await message_processor.audio_to_text_n8n(audio_data)
                    if text:
                        logger.info(f"📢 Áudio transcrito: {text}")
                    else:
                        logger.warning("❌ Falha na transcrição do áudio")
                        text = "Desculpe, não consegui entender o áudio."
                else:
                    logger.debug(f"📄 Chaves disponíveis no objeto message: {list(message_obj.keys())}")
                    if "conversation" in message_obj:
                        text = message_obj["conversation"]
                        logger.debug(f"📄 Texto extraído de 'conversation': {text[:50]}...")
                    elif "extendedTextMessage" in message_obj:
                        text = message_obj["extendedTextMessage"].get("text", "")
                        logger.debug(f"📄 Texto extraído de 'extendedTextMessage': {text[:50]}...")
                    elif "buttonsResponseMessage" in message_obj:
                        # Extrair texto de respostas de botões
                        text = message_obj["buttonsResponseMessage"].get("selectedButtonId", "")
                        logger.debug(f"📄 Resposta de botão detectada: {text}")
                    elif "templateButtonReplyMessage" in message_obj:
                        # Extrair texto de respostas de template
                        text = message_obj["templateButtonReplyMessage"].get("selectedId", "")
                        logger.debug(f"📄 Resposta de template detectada: {text}")
                    elif "listResponseMessage" in message_obj:
                        # Extrair texto de respostas de lista
                        text = message_obj["listResponseMessage"].get("title", "")
                        logger.debug(f"📄 Resposta de lista detectada: {text}")
                    else:
                        logger.warning("⚠️ Nenhum campo de texto reconhecido encontrado no objeto message")
                        logger.debug(f"📄 Estrutura completa do objeto message: {json.dumps(message_obj, default=str, indent=2)[:500]}...")
                
                logger.info(f"📩 Mensagem extraída: Remetente={sender}, Texto={text}")
                
                # Verificação para texto vazio mas com outros dados
                if not text and message_obj:
                    logger.warning("⚠️ Texto vazio mas objeto message contém dados")
                    # Tentar extrair qualquer texto disponível em outros campos
                    for key, value in message_obj.items():
                        if isinstance(value, dict) and "text" in value:
                            text = value["text"]
                            logger.info(f"📄 Texto alternativo encontrado em {key}: {text}")
                            break
                    
                    # Se ainda estiver vazio, usar um valor padrão para processamento
                    if not text:
                        logger.warning("⚠️ Definindo texto padrão para mensagem vazia")
                        text = "[Mensagem sem texto]"
            else:
                logger.warning("Array de mensagens vazio ou inválido")
                return {"status": "error", "message": "Formato de mensagem inválido"}
        
        # Verificar formato alternativo (webhook direto)
        elif "key" in data and "remoteJid" in data["key"]:
            message_data = data
            sender = message_data["key"]["remoteJid"]
            
            message_obj = message_data.get("message", {})
            text = ""
            
            if "conversation" in message_obj:
                text = message_obj["conversation"]
            elif "extendedTextMessage" in message_obj:
                text = message_obj["extendedTextMessage"].get("text", "")
            
            logger.info(f"📩 Mensagem extraída (formato alternativo): Remetente={sender}, Texto={text}")
            # Verificação detalhada dos dados extraídos
            logger.debug(f"Detalhes do remetente: Tipo={type(sender)}, Vazio={sender == ''}, Valor={sender}")
            logger.debug(f"Detalhes do texto: Tipo={type(text)}, Vazio={text == ''}, Tamanho={len(text) if text else 0}")
        
        # Verificar outros formatos possíveis
        elif "data" in data and isinstance(data["data"], dict):
            message_data = data["data"]
            
            # Verificar se contém as informações necessárias
            if "key" in message_data and "remoteJid" in message_data["key"]:
                sender = message_data["key"]["remoteJid"]
                
                message_obj = message_data.get("message", {})
                text = ""
                
                # Detectar e processar áudio no formato data
                if "audioMessage" in message_obj:
                    logger.info(f"🎤 Detectada mensagem de áudio no formato 'data'")
                    logger.debug(f"🎤 Estrutura do audioMessage: {json.dumps(message_obj['audioMessage'], default=str)[:300]}...")
                    
                    try:
                        audio_data = message_obj["audioMessage"]
                        
                        # Verificar se existe uma representação base64 no webhook
                        if "base64" in message_data:
                            logger.info("🎤 Usando representação base64 direta do formato data")
                            audio_data["base64"] = message_data["base64"]
                        elif "base64" in message_obj:
                            logger.info("🎤 Usando representação base64 do objeto message")
                            audio_data["base64"] = message_obj["base64"]
                        
                        logger.info(f"🎤 Iniciando transcrição de áudio (formato data)")
                        text = await message_processor.audio_to_text_n8n(audio_data)
                        
                        if text:
                            logger.info(f"🎤 Áudio transcrito com sucesso (formato data): {text}")
                        else:
                            logger.warning("❌ Falha na transcrição do áudio (formato data)")
                            text = "Desculpe, não consegui entender o áudio."
                    except Exception as audio_err:
                        logger.error(f"❌ Erro ao processar mensagem de áudio (formato data): {str(audio_err)}")
                        logger.exception("Detalhes do erro:")
                        text = "Desculpe, houve um erro ao processar o áudio."
                elif "conversation" in message_obj:
                    text = message_obj["conversation"]
                elif "extendedTextMessage" in message_obj:
                    text = message_obj["extendedTextMessage"].get("text", "")
                
                logger.info(f"📩 Mensagem extraída (formato data): Remetente={sender}, Texto={text}")
                # Verificação detalhada dos dados extraídos
                logger.debug(f"Detalhes do remetente (formato data): Tipo={type(sender)}, Vazio={sender == ''}, Valor={sender}")
                logger.debug(f"Detalhes do texto (formato data): Tipo={type(text)}, Vazio={text == ''}, Tamanho={len(text) if text else 0}")
            else:
                logger.warning("Dados em formato 'data' sem estrutura válida de mensagem")
                logger.debug(f"Estrutura do objeto data: {json.dumps(message_data, indent=2)}")
                return {"status": "error", "message": "Dados em formato inválido"}
        else:
            # Se nenhum formato conhecido for encontrado, tentar uma busca recursiva
            logger.warning("Formato de dados desconhecido, tentando busca recursiva")
            
            # Função auxiliar para buscar recursivamente
            def find_message_data(obj, depth=0):
                if depth > 5:  # Limite para evitar recursão infinita
                    return None
                
                if isinstance(obj, dict):
                    # Primeiro caso: objeto tem key/remoteJid e message
                    if "key" in obj and "remoteJid" in obj.get("key", {}) and "message" in obj:
                        logger.debug(f"🔍 Encontrada estrutura de mensagem válida na profundidade {depth}")
                        return obj
                    
                    # Procurar em todos os campos do objeto
                    for key, value in obj.items():
                        # Log para campos potencialmente importantes
                        if key in ["key", "message", "data", "messages"]:
                            logger.debug(f"🔍 Verificando campo potencial '{key}' na profundidade {depth}")
                        
                        result = find_message_data(value, depth + 1)
                        if result:
                            return result
                
                if isinstance(obj, list):
                    logger.debug(f"🔍 Verificando lista com {len(obj)} itens na profundidade {depth}")
                    for item in obj:
                        result = find_message_data(item, depth + 1)
                        if result:
                            return result
                
                return None
            
            # Tentar encontrar os dados da mensagem
            logger.info("🔍 Iniciando busca recursiva para encontrar estrutura de mensagem")
            message_data = find_message_data(data)
            
            if message_data:
                sender = message_data["key"]["remoteJid"]
                
                message_obj = message_data.get("message", {})
                text = ""
                
                logger.debug(f"🔄 Estrutura encontrada na busca recursiva: {json.dumps(message_data.get('key', {}), default=str)}")
                logger.debug(f"🔄 Objeto message encontrado: {json.dumps(message_obj, default=str, indent=2)[:200]}...")
                
                # Verificar se contém uma mensagem de áudio
                if "audioMessage" in message_obj:
                    logger.info(f"🎤 Detectada mensagem de áudio na busca recursiva")
                    logger.debug(f"🎤 Estrutura do audioMessage (busca recursiva): {json.dumps(message_obj['audioMessage'], default=str)[:300]}...")
                    
                    try:
                        audio_data = message_obj["audioMessage"]
                        
                        # Verificar se existe uma representação base64 no webhook
                        if "base64" in message_data:
                            logger.info("🎤 Usando representação base64 direta na busca recursiva")
                            audio_data["base64"] = message_data["base64"]
                        elif "base64" in message_obj:
                            logger.info("🎤 Usando representação base64 do objeto message (busca recursiva)")
                            audio_data["base64"] = message_obj["base64"]
                        
                        logger.info(f"🎤 Iniciando transcrição de áudio (busca recursiva)")
                        text = await message_processor.audio_to_text_n8n(audio_data)
                        
                        if text:
                            logger.info(f"🎤 Áudio transcrito com sucesso (busca recursiva): {text}")
                        else:
                            logger.warning("❌ Falha na transcrição do áudio (busca recursiva)")
                            text = "Desculpe, não consegui entender o áudio."
                    except Exception as audio_err:
                        logger.error(f"❌ Erro ao processar mensagem de áudio (busca recursiva): {str(audio_err)}")
                        logger.exception("Detalhes do erro:")
                        text = "Desculpe, houve um erro ao processar o áudio."
                elif "conversation" in message_obj:
                    text = message_obj["conversation"]
                    logger.debug(f"🔄 Texto extraído do campo 'conversation': {text[:50]}...")
                elif "extendedTextMessage" in message_obj:
                    text = message_obj["extendedTextMessage"].get("text", "")
                    logger.debug(f"🔄 Texto extraído do campo 'extendedTextMessage': {text[:50]}...")
                
                logger.info(f"📩 Mensagem extraída (busca recursiva): Remetente={sender}, Texto={text}")
                # Verificação detalhada dos dados extraídos
                logger.debug(f"Detalhes do remetente (busca recursiva): Tipo={type(sender)}, Vazio={sender == ''}, Valor={sender}")
                logger.debug(f"Detalhes do texto (busca recursiva): Tipo={type(text)}, Vazio={text == ''}, Tamanho={len(text) if text else 0}")
            else:
                logger.error(f"Não foi possível extrair dados da mensagem: {json.dumps(data, default=str)[:300]}...")
                return {"status": "error", "message": "Formato de dados não reconhecido"}
        
        # Verificar se temos os dados necessários para continuar
        if not sender:
            logger.error("Remetente ausente no processamento da mensagem")
            logger.debug(f"Estrutura da mensagem processada: {json.dumps(message_data, default=str, indent=2)}")
            return {"status": "error", "message": "Dados incompletos: remetente ausente"}
        
        if not text:
            logger.error("Texto da mensagem ausente no processamento")
            logger.debug(f"Remetente identificado: {sender}")
            logger.debug(f"Estrutura de message_obj: {json.dumps(message_obj, default=str, indent=2)}")
            return {"status": "error", "message": "Dados incompletos: texto ausente"}
        
        logger.info(f"📱 Processando mensagem de {sender}: {text}")
        
        # O resto do código continua a partir daqui, com os valores sender e text já extraídos
        
        if sender == "Julia Atendimento":
            return {"status": "ignored"}
            
        # 📌 Extrair informações da mensagem
        cidade_match = re.search(r"(Teresina|Guadalupe)", text, re.IGNORECASE)
        bairro_match = re.search(r"bairro\s+(\w+)", text, re.IGNORECASE)
        zona_match = re.search(r"zona\s+(urbana|rural)", text, re.IGNORECASE)
        
        cidade = cidade_match.group(1) if cidade_match else None
        bairro = bairro_match.group(1) if bairro_match else None
        zona = zona_match.group(1) if zona_match else None

        # 📌 Verificar cliente existente e extrair informações
        user_data = {}
        try:
            cliente_query = supabase.table("cliente_cadastro").select("*").eq("telefone", sender).execute()
            if len(cliente_query.data) > 0:
                # Usar dados do cliente existente
                cliente = cliente_query.data[0]
                user_data = {
                    "telefone": sender,
                    "nome": cliente.get("nome", ""),
                    "cidade": cliente.get("cidade", ""),
                    "bairro": cliente.get("bairro", ""),
                }
                
                logger.info(f"🔍 Cliente existente encontrado: {cliente.get('nome', 'Sem nome')} - {sender}")
                logger.debug(f"🔍 Dados do cliente: Cidade={user_data['cidade']}, Bairro={user_data['bairro']}")
                
                # Completar com dados da mensagem atual se não existirem
                if not user_data["cidade"] and cidade:
                    user_data["cidade"] = cidade
                    logger.debug(f"🔄 Atualizando cidade do cliente para: {cidade}")
                if not user_data["bairro"] and bairro:
                    user_data["bairro"] = bairro
                    logger.debug(f"🔄 Atualizando bairro do cliente para: {bairro}")
            else:
                logger.info(f"🔍 Cliente novo: {sender}")
        except Exception as e:
            logger.error(f"Erro ao consultar cliente: {str(e)}")
            user_data = {"telefone": sender}
            if cidade:
                user_data["cidade"] = cidade
            if bairro:
                user_data["bairro"] = bairro
        
        # 📌 Verificar cobertura se tiver cidade
        if "cidade" in user_data:
            logger.info(f"🔍 Verificando cobertura para: Cidade={user_data['cidade']}, Bairro={user_data.get('bairro', 'N/A')}")
            cobertura, planos = verificar_cobertura(
                user_data["cidade"], 
                user_data.get("bairro"), 
                zona
            )
            user_data["cobertura"] = cobertura
            if cobertura is True:
                logger.info(f"✅ Cobertura confirmada para {user_data['cidade']}")
            elif cobertura is False:
                logger.info(f"❌ Sem cobertura para {user_data['cidade']}")
            else:
                logger.info(f"⚠️ Verificação de cobertura inconclusiva para {user_data['cidade']}")
            
            if planos:
                user_data["planos"] = planos
                logger.debug(f"📋 Planos disponíveis: {json.dumps(planos)}")

        # 📌 Gerar resposta com o novo contexto e chat memory
        logger.info(f"💬 Gerando resposta AI para: {sender}")
        resposta_ai = await generate_ai_response(text, user_data, session_id=sender)
        
        # 📌 Enviar resposta pelo WhatsApp
        try:
            response = send_text_message(sender, resposta_ai)
            logger.info(f"✅ Mensagem enviada com sucesso para {sender}")
        except Exception as e:
            logger.error(f"❌ Falha ao enviar mensagem via Evolution API: {str(e)}")
            # Informar ao cliente sobre o problema
            return {
                "status": "error", 
                "message": "Falha ao enviar mensagem",
                "response": resposta_ai
            }

        # 📌 Verificar se a IA indicou que o cadastro está completo
        cadastro_completo = False
        indicadores_conclusao = [
            "encaminhando sua solicitação",
            "entrará em contato em breve",
            "agendar a instalação",
            "obrigado pela confiança",
            "agradecemos pela preferência",
            "cadastro concluído"
        ]
        
        if any(indicador.lower() in resposta_ai.lower() for indicador in indicadores_conclusao):
            # Verificar se temos todos os dados necessários para um cadastro completo
            campos_obrigatorios = ["nome", "cpf", "telefone", "cidade", "bairro", "plano_escolhido"]
            
            # Extrair/atualizar informações que podem estar na mensagem atual
            if "nome" not in user_data or not user_data["nome"]:
                nome_match = re.search(r"(?:me\s+chamo|sou|nome[:\s]+)\s*([a-zA-Z\s]{2,50})", text, re.IGNORECASE)
                if nome_match:
                    user_data["nome"] = nome_match.group(1).strip()
            
            if "cpf" not in user_data or not user_data["cpf"]:
                cpf_match = re.search(r"(?:cpf[:\s]+|documento[:\s]+)?\s*(\d{11})", text, re.IGNORECASE)
                if cpf_match:
                    user_data["cpf"] = cpf_match.group(1)
            
            if "plano_escolhido" not in user_data or not user_data["plano_escolhido"]:
                plano_match = re.search(r"(?:plano|quero|contratar)[:\s]*(100|200|300|básico|intermediário|premium|primeiro|segundo|terceiro)", text, re.IGNORECASE)
                if plano_match:
                    plano_texto = plano_match.group(1).lower()
                    if "100" in plano_texto or "básico" in plano_texto or "primeiro" in plano_texto:
                        user_data["plano_escolhido"] = "100MB"
                    elif "200" in plano_texto or "intermediário" in plano_texto or "segundo" in plano_texto:
                        user_data["plano_escolhido"] = "200MB"
                    elif "300" in plano_texto or "premium" in plano_texto or "terceiro" in plano_texto:
                        user_data["plano_escolhido"] = "300MB"
            
            # Verificar se todos os campos obrigatórios estão preenchidos
            dados_presentes = all(campo in user_data and user_data[campo] for campo in campos_obrigatorios)
            
            # Verificar com IA se o cadastro parece completo
            prompt_verificacao = f"""
            Analise as informações disponíveis e determine se temos um cadastro completo:
            
            Nome: {user_data.get('nome', 'Não informado')}
            CPF: {user_data.get('cpf', 'Não informado')}
            Telefone: {user_data.get('telefone', 'Não informado')}
            Cidade: {user_data.get('cidade', 'Não informado')}
            Bairro: {user_data.get('bairro', 'Não informado')}
            Plano escolhido: {user_data.get('plano_escolhido', 'Não informado')}
            
            Responda apenas com "COMPLETO" se todas as informações necessárias para instalação estão disponíveis
            ou "INCOMPLETO" se faltam informações essenciais.
            """
            
            try:
                response = client.chat.completions.create(
                    model="gpt-3.5-turbo",  # Modelo mais leve para esta verificação
                    messages=[{"role": "system", "content": prompt_verificacao}]
                )
                verificacao_ia = response.choices[0].message.content.strip()
                logger.info(f"Verificação IA para cadastro: {verificacao_ia}")
                
                cadastro_completo = dados_presentes and "COMPLETO" in verificacao_ia
            except Exception as e:
                logger.error(f"Erro na verificação de IA do cadastro: {str(e)}")
                # Cair na verificação programática se a IA falhar
                cadastro_completo = dados_presentes
        
        # 📌 Salvar no Supabase
        try:
            mensagem_data = {
                "telefone": sender, 
                "mensagem": text, 
                "resposta": resposta_ai,
                "data_hora": datetime.now().isoformat()
            }
            if is_cadastro_completo(user_data):
                supabase.table("mensagens").insert(mensagem_data).execute()
                logger.info("✅ Mensagem registrada com sucesso")
            else:
                logger.info("⏸️ Mensagem não registrada por cadastro incompleto")

            logger.info("✅ Mensagem registrada com sucesso")
        except Exception as e:
            logger.warning(f"⚠️ Não foi possível registrar a mensagem (tabela pode não existir): {str(e)}")

        # Atualizar ou inserir cliente SOMENTE SE cadastro estiver completo
        if is_cadastro_completo(user_data):
            try:
                cliente_update = {
                    "telefone": sender      
                }

                for campo in ["nome", "cpf", "cidade", "bairro", "plano_escolhido"]:
                    if campo in user_data and user_data[campo]:
                        cliente_update[campo] = user_data[campo]

                supabase.table("cliente_cadastro").upsert(cliente_update).execute()
                logger.info("✅ Dados do cliente salvos/atualizados")
            except Exception as e:
                logger.error(f"❌ Erro ao salvar/atualizar cliente: {str(e)}")
        else:
            logger.info("⏸️ Dados ainda incompletos. Cadastro não salvo.")



        # Cadastro completo
        if cadastro_completo:
            try:
                logger.info(f"✅ Cadastro completo detectado para {sender}")
                user_data["status"] = "pendente_instalacao"
                save_client_data(user_data)
            except Exception as e:
                logger.error(f"❌ Falha ao salvar cadastro completo: {str(e)}")


        return {"status": "success", "response": resposta_ai}
    
    except Exception as e:
        logger.error(f"Erro no webhook: {str(e)}")
        raise HTTPException(status_code=500, detail="Erro interno no servidor")

# 📌 Função para enviar mensagens via API
def send_text_message(number, text):
    try:
        # Verificar se o texto está vazio
        if not text or text.strip() == "":
            logger.error("❌ Texto vazio não pode ser enviado")
            return {"status": "error", "message": "Texto vazio não pode ser enviado"}
            
        # Verificar se o serviço está disponível
        if not evolution_service:
            logger.error("❌ Serviço Evolution API não está inicializado")
            raise Exception("Serviço Evolution API não está inicializado")
            
        # Padronizar o número de telefone
        numero_padronizado = padronizar_telefone(number)
        
        # Log detalhado para diagnóstico
        logger.info(f"📤 Enviando mensagem via Evolution API para {numero_padronizado}")
        logger.debug(f"📤 Conteúdo da mensagem: {text[:50]}...")
        
        # Enviar a mensagem usando o serviço Evolution API
        response = evolution_service.send_text_message(numero_padronizado, text)
        
        # Verificar resposta
        if response.get("status") != "success":
            logger.error(f"❌ Falha ao enviar mensagem via Evolution API: {response.get('message')}")
            raise Exception(f"Falha no envio: {response.get('message')}")
        
        logger.info(f"✅ Mensagem enviada com sucesso via Evolution API para {numero_padronizado}")
        return response
    except Exception as e:
        logger.error(f"❌ Erro crítico ao enviar mensagem: {str(e)}")
        raise  # Propagar o erro para ser tratado pelo chamador

# 📌 Função para verificar números de WhatsApp
def verify_whatsapp_numbers(numeros):
    response = evolution_service.verify_whatsapp_numbers(numeros)
    if response["status"] != "success":
        logger.error(f"Erro ao verificar números: {response.get('message', 'Erro desconhecido')}")
        return []
    
    logger.info(f"✅ Verificação de números concluída com sucesso")
    return response.get("data", {}).get("valid", [])

# 📌 Rodar servidor
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)


