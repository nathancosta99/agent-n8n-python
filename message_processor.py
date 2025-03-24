import logging
import httpx
import tempfile
import os
import base64
from typing import Optional
import os


# Configurar Logs
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Definir a classe MessageProcessor
class MessageProcessor:
    async def audio_to_text(self, audio_data: dict) -> Optional[str]:
        try:
            logger.info("Iniciando conversão de áudio para texto")
            
            # Verificar e logar o conteúdo de audio_data
            logger.info(f"Dados de áudio recebidos: {audio_data}")
            
            if "base64" in audio_data:
                audio_base64 = audio_data["base64"]
                logger.info(f"Tamanho do base64: {len(audio_base64)} caracteres")
            else:
                logger.error("Conteúdo base64 não encontrado na mensagem de áudio")
                return None
                
            # Decodificar o base64
            audio_content = base64.b64decode(audio_base64)
            logger.info(f"Tamanho do áudio decodificado: {len(audio_content)} bytes")
            
            # Criar arquivo temporário
            with tempfile.NamedTemporaryFile(delete=False, suffix=".ogg") as temp_file:
                temp_file.write(audio_content)
                temp_path = temp_file.name
                logger.info(f"Arquivo temporário criado: {temp_path}")
            
            try:
                # Fazer requisição à API da OpenAI
                async with httpx.AsyncClient() as client:
                    with open(temp_path, "rb") as audio_file:
                        stt_response = await client.post(
                            "https://api.openai.com/v1/audio/transcriptions",
                            headers={"Authorization": f"Bearer {os.getenv('OPENAI_API_KEY')}"},
                            files={"file": ("audio.ogg", audio_file, "audio/ogg")},
                            data={"model": "whisper-1"}
                        )
                        logger.info(f"Status code da resposta: {stt_response.status_code}")
                        logger.info(f"Resposta da API: {stt_response.text}")
                        if stt_response.status_code == 200:
                            text = stt_response.json().get("text", "")
                            logger.info(f"Transcrição concluída: '{text}'")
                            return text
                        else:
                            logger.error(f"Erro na transcrição: {stt_response.text}")
                            return None
            finally:
                # Limpar arquivo temporário
                if os.path.exists(temp_path):
                    os.unlink(temp_path)
                    logger.info("Arquivo temporário removido")
        except Exception as e:
            logger.error(f"Erro na conversão de áudio para texto: {str(e)}")
            return None