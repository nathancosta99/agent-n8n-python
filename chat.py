import streamlit as st
import requests
import json
import os
import uuid

# 🔹 URL da API do chatbot
API_URL = "http://localhost:8000/webhook"  # Ajuste conforme necessário

# 🔹 Configurar Streamlit
st.set_page_config(page_title="Chat Julia - SMNET", layout="wide")
st.title("💬 Chatbot Julia - SMNET")

# 🔹 Inicializar histórico de mensagens e ID de sessão
if "messages" not in st.session_state:
    st.session_state["messages"] = []

# 🔹 Criar um ID de sessão único e persistente
if "session_id" not in st.session_state:
    st.session_state["session_id"] = f"streamlit_{uuid.uuid4()}"

# 🔹 Exibir histórico de mensagens
for msg in st.session_state["messages"]:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

# 🔹 Caixa de entrada para o usuário
user_input = st.chat_input("Digite sua mensagem...")

# 🔹 Quando o usuário envia uma mensagem
if user_input:
    # 📌 Adicionar mensagem do usuário ao histórico
    st.session_state["messages"].append({"role": "user", "content": user_input})
    with st.chat_message("user"):
        st.markdown(user_input)

    # 📌 Mostrar indicador de carregamento
    with st.chat_message("assistant"):
        message_placeholder = st.empty()
        message_placeholder.info("Julia está digitando...")
        
        try:
            # 📌 Enviar mensagem para a API
            payload = {
                "body": {
                    "data": {
                        "key": {"remoteJid": st.session_state["session_id"]},
                        "message": {"messageType": "conversation"},
                        "text": user_input
                    }
                }
            }
            
            response = requests.post(API_URL, json=payload)
            
            if response.status_code == 200:
                response_data = response.json()
                resposta = response_data.get("response", response_data.get("status", "Sem resposta"))
            else:
                resposta = f"Erro ao se conectar com o chatbot. Status: {response.status_code}"
        except Exception as e:
            resposta = f"Erro na comunicação com o servidor: {str(e)}"

        # Substituir o indicador de carregamento pela resposta real
        message_placeholder.markdown(resposta)
        
    # 📌 Adicionar resposta do chatbot ao histórico
    st.session_state["messages"].append({"role": "assistant", "content": resposta})
