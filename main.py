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

# 🔹 Carregar variáveis de ambiente
load_dotenv()

# 🔹 Configurar Logs
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

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


# 🔹 Configurar API de envio de mensagens
API_URL = "https://evolutionv2.datalabpesquisas.com/message/sendText/agente-n8n-python"
API_KEY = os.getenv("API_KEY")

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
                
            # Consultar a tabela Clientes_cadastro para verificar cobertura no bairro
            query = supabase.table("Clientes_cadastro").select("*").eq("cidade", cidade).eq("bairro", bairro).execute()
            
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
        logger.error(f"Erro ao verificar cobertura usando Clientes_cadastro: {str(e)}")
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

# 📌 Função para salvar cadastro do cliente
def save_client_data(data):
    try:
        # Salvar dados completos do cliente
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
            "status": "pendente_instalacao",
            "data_cadastro": "now()"
        }
        
        supabase.table("Clientes_cadastro").upsert(cliente_data).execute()
        logger.info(f"✅ Cadastro do cliente salvo com sucesso: {data['telefone']}")
        return True
    except Exception as e:
        logger.error(f"Erro ao salvar cadastro do cliente: {str(e)}")
        return False

# 📌 Webhook para receber mensagens
@app.post("/webhook")
async def receive_message(request: Request):
    try:
        data = await request.json()
        message = MessageData(**data["body"]["data"])

        sender = message.key["remoteJid"]
        text = message.text if message.text else ""

        logger.info(f"📩 Mensagem recebida de {sender}: {text}")

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
            cliente_query = supabase.table("Clientes_cadastro").select("*").eq("telefone", sender).execute()
            if len(cliente_query.data) > 0:
                # Usar dados do cliente existente
                cliente = cliente_query.data[0]
                user_data = {
                    "telefone": sender,
                    "nome": cliente.get("nome", ""),
                    "cidade": cliente.get("cidade", ""),
                    "bairro": cliente.get("bairro", ""),
                }
                
                # Completar com dados da mensagem atual se não existirem
                if not user_data["cidade"] and cidade:
                    user_data["cidade"] = cidade
                if not user_data["bairro"] and bairro:
                    user_data["bairro"] = bairro
        except Exception as e:
            logger.error(f"Erro ao consultar cliente: {str(e)}")
            user_data = {"telefone": sender}
            if cidade:
                user_data["cidade"] = cidade
            if bairro:
                user_data["bairro"] = bairro
        
        # 📌 Verificar cobertura se tiver cidade
        if "cidade" in user_data:
            cobertura, planos = verificar_cobertura(
                user_data["cidade"], 
                user_data.get("bairro"), 
                zona
            )
            user_data["cobertura"] = cobertura
            if planos:
                user_data["planos"] = planos

        # 📌 Gerar resposta com o novo contexto e chat memory
        resposta_ai = await generate_ai_response(text, user_data, session_id=sender)
        
        # 📌 Enviar resposta pelo WhatsApp
        try:
            send_text_message(sender, resposta_ai)
        except Exception as e:
            logger.error(f"Falha ao enviar mensagem: {str(e)}")
            return {"status": "partial_success", "response": resposta_ai}

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
            # Salvar mensagem
            mensagem_data = {
                "telefone": sender, 
                "mensagem": text, 
                "resposta": resposta_ai,
                "data_hora": "now()"
            }
            supabase.table("mensagens").insert(mensagem_data).execute()
            
            # Sempre atualizar os dados parciais do cliente
            cliente_update = {
                "telefone": sender,
                "ultima_interacao": "now()"
            }
            
            # Adicionar todos os dados disponíveis
            for campo in ["nome", "cpf", "cidade", "bairro", "plano_escolhido"]:
                if campo in user_data and user_data[campo]:
                    cliente_update[campo] = user_data[campo]
            
            # Upsert para atualizar/criar registro com dados parciais
            supabase.table("Clientes_cadastro").upsert(cliente_update).execute()
            
            # Salvar cadastro completo se todas as verificações passarem
            if cadastro_completo:
                logger.info(f"✅ Cadastro completo detectado para {sender}")
                user_data["status"] = "pendente_instalacao"
                save_client_data(user_data)
                logger.info(f"✅ Dados completos salvos para instalação!")
        except Exception as e:
            logger.error(f"Falha ao salvar no Supabase: {str(e)}")

        return {"status": "success", "response": resposta_ai}
    
    except Exception as e:
        logger.error(f"Erro no webhook: {str(e)}")
        raise HTTPException(status_code=500, detail="Erro interno no servidor")

# 📌 Função para enviar mensagens via API
def send_text_message(number, text):
    payload = {"number": number, "text": text}
    send_request(payload)

# 📌 Função genérica para envio de requisição
def send_request(payload):
    try:
        headers = {"apikey": API_KEY, "Content-Type": "application/json"}
        # Adicionar mais informações de log para depuração
        logger.info(f"Enviando payload: {payload}")
        logger.info(f"Headers: {headers}")
        logger.info(f"URL da API: {API_URL}")
        
        response = requests.post(API_URL, json=payload, headers=headers)
        
        # Logar resposta completa para depuração
        logger.info(f"Resposta da API: {response.status_code}, Conteúdo: {response.text}")
        
        response.raise_for_status()
        logger.info("✅ Mensagem enviada com sucesso!")
    except requests.exceptions.RequestException as e:
        logger.error(f"Erro ao enviar mensagem: {str(e)}")
        # Adicionar mais detalhes do erro se disponíveis
        if hasattr(e, 'response') and e.response:
            logger.error(f"Detalhes do erro: {e.response.text}")
        raise

# 📌 Rodar servidor
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
