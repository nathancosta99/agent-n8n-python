import logging
import httpx
import tempfile
import os
import base64
import subprocess
import time
from typing import Optional
import os
import json


# Configurar Logs
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Definir a classe MessageProcessor
class MessageProcessor:
    def __init__(self):
        # Verificar se o FFmpeg está instalado
        try:
            result = subprocess.run(['ffmpeg', '-version'], 
                                  stdout=subprocess.PIPE, 
                                  stderr=subprocess.PIPE)
            logger.info("✅ FFmpeg encontrado no sistema")
            self.ffmpeg_available = True
        except (FileNotFoundError, subprocess.SubprocessError):
            logger.warning("⚠️ FFmpeg não encontrado no sistema. A conversão de áudio pode falhar.")
            self.ffmpeg_available = False

    def convert_audio(self, input_file: str, output_format: str = "mp3") -> Optional[str]:
        """
        Converte um arquivo de áudio para um formato compatível com a API de transcrição.
        
        Args:
            input_file: Caminho para o arquivo de entrada
            output_format: Formato de saída desejado (mp3, wav, etc.)
            
        Returns:
            Caminho para o arquivo convertido ou None se falhar
        """
        if not self.ffmpeg_available:
            logger.error("❌ FFmpeg não disponível para conversão de áudio")
            return None
            
        try:
            # Criar nome para arquivo de saída
            output_file = f"{input_file}.{output_format}"
            
            # Comando para converter o áudio
            command = [
                'ffmpeg',
                '-i', input_file,
                '-y',  # Sobrescrever arquivo de saída se existir
                '-c:a', 'libmp3lame' if output_format == 'mp3' else 'pcm_s16le',
                '-ar', '16000',  # Taxa de amostragem de 16kHz (ideal para Whisper)
                '-ac', '1',      # Mono
                '-b:a', '128k',  # Bitrate de 128kbps
                output_file
            ]
            
            logger.debug(f"🔄 Convertendo áudio: {' '.join(command)}")
            
            # Executar a conversão
            process = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False
            )
            
            if process.returncode != 0:
                logger.error(f"❌ Erro na conversão do áudio: {process.stderr}")
                return None
                
            logger.info(f"✅ Áudio convertido com sucesso para {output_format}")
            return output_file
            
        except Exception as e:
            logger.error(f"❌ Erro na conversão do áudio: {str(e)}")
            return None

    async def audio_to_text(self, audio_data: dict) -> Optional[str]:
        try:
            logger.info("🎤 Iniciando conversão de áudio para texto")
            
            # Log das chaves disponíveis para diagnóstico
            logger.info(f"🎤 Chaves disponíveis em audio_data: {list(audio_data.keys())}")
            logger.debug(f"🎤 Dados completos de áudio: {json.dumps(audio_data, default=str)[:300]}...")
            
            # Definir variáveis para controle do fluxo
            temp_path = None
            converted_path = None
            audio_content = None
            
            # Corrigido: Implementação mais robusta para processar o conteúdo base64
            if "base64" in audio_data:
                try:
                    # Caso 1: o base64 está diretamente no objeto audioMessage
                    audio_base64 = audio_data["base64"]
                    logger.info(f"🎤 Usando base64 do objeto audio_data, tamanho: {len(audio_base64)} caracteres")
                    
                    # Sanitizar a string base64 - remover possíveis caracteres inválidos
                    # Às vezes pode vir com prefixos como "data:audio/ogg;base64,"
                    if "," in audio_base64:
                        audio_base64 = audio_base64.split(",", 1)[1]
                    
                    # Sanitizar a string para evitar caracteres inválidos no base64
                    audio_base64 = audio_base64.replace(" ", "").replace("\n", "").replace("\r", "")
                    
                    # Garantir que o padding está correto
                    padding = len(audio_base64) % 4
                    if padding:
                        audio_base64 += "=" * (4 - padding)
                    
                    # Tentar decodificar com tratamento adequado
                    try:
                        audio_content = base64.b64decode(audio_base64)
                        logger.info(f"🎤 Decodificação de base64 bem-sucedida, tamanho: {len(audio_content)} bytes")
                    except Exception as decode_err:
                        logger.error(f"❌ Erro na decodificação base64: {str(decode_err)}")
                        audio_content = None
                except Exception as base64_err:
                    logger.error(f"❌ Erro ao processar base64: {str(base64_err)}")
                    audio_content = None
                    
            # Tentar outras fontes de dados se a base64 falhar
            if audio_content is None and "ptt" in audio_data and isinstance(audio_data["ptt"], dict) and "data" in audio_data["ptt"]:
                # Caso 2: o base64 está dentro do campo ptt (comum em algumas versões do WhatsApp)
                try:
                    logger.info("🎤 Tentando extrair base64 do campo 'ptt'")
                    audio_base64 = audio_data["ptt"]["data"]
                    logger.info(f"🎤 Tamanho do base64 de ptt: {len(audio_base64)} caracteres")
                    
                    # Sanitizar e decodificar
                    if "," in audio_base64:
                        audio_base64 = audio_base64.split(",", 1)[1]
                    
                    audio_base64 = audio_base64.replace(" ", "").replace("\n", "").replace("\r", "")
                    padding = len(audio_base64) % 4
                    if padding:
                        audio_base64 += "=" * (4 - padding)
                        
                    audio_content = base64.b64decode(audio_base64)
                    logger.info(f"🎤 Decodificação do campo 'ptt' bem-sucedida, tamanho: {len(audio_content)} bytes")
                except Exception as ptt_err:
                    logger.warning(f"🎤 Erro ao extrair base64 do campo 'ptt': {str(ptt_err)}")
                    
            # Tentar body se as opções anteriores falharem
            if audio_content is None and "body" in audio_data:
                try:
                    logger.info("🎤 Tentando extrair base64 do campo 'body'")
                    audio_base64 = audio_data["body"]
                    
                    # Sanitizar e decodificar
                    if "," in audio_base64:
                        audio_base64 = audio_base64.split(",", 1)[1]
                    
                    audio_base64 = audio_base64.replace(" ", "").replace("\n", "").replace("\r", "")
                    padding = len(audio_base64) % 4
                    if padding:
                        audio_base64 += "=" * (4 - padding)
                        
                    audio_content = base64.b64decode(audio_base64)
                    logger.info(f"🎤 Áudio extraído do campo 'body', tamanho: {len(audio_content)} bytes")
                except Exception as body_err:
                    logger.warning(f"🎤 Erro ao extrair base64 do campo 'body': {str(body_err)}")
            
            # Se até agora não temos o conteúdo, tentar baixar da URL
            if audio_content is None and "url" in audio_data:
                logger.info(f"🎤 Base64 não encontrado, tentando baixar da URL: {audio_data['url']}")
                try:
                    async with httpx.AsyncClient(timeout=30.0) as client:
                        response = await client.get(audio_data["url"])
                        if response.status_code == 200:
                            audio_content = response.content
                            logger.info(f"🎤 Áudio baixado da URL, tamanho: {len(audio_content)} bytes")
                        else:
                            logger.error(f"❌ Erro ao baixar áudio da URL: {response.status_code} - {response.text}")
                except Exception as url_err:
                    logger.error(f"❌ Erro ao processar áudio da URL: {str(url_err)}")
                    
            # Última tentativa: directPath
            if audio_content is None and "directPath" in audio_data:
                # Detectando URL potencial da Evolution API/WhatsApp
                base_url = "https://mmg.whatsapp.net"
                direct_path = audio_data["directPath"]
                
                if direct_path.startswith("/"):
                    full_url = f"{base_url}{direct_path}"
                else:
                    full_url = f"{base_url}/{direct_path}"
                
                logger.info(f"🎤 Tentando baixar áudio via directPath: {full_url}")
                
                try:
                    async with httpx.AsyncClient(timeout=30.0) as client:
                        # Adicionando headers específicos que podem ser necessários
                        headers = {
                            "User-Agent": "WhatsApp/2.21.12.21",
                            "Accept": "*/*"
                        }
                        
                        # Se houver mediaKey disponível, usar para autenticação
                        if "mediaKey" in audio_data:
                            logger.info("🎤 Usando mediaKey para autenticação")
                            headers["Authorization"] = f"Bearer {audio_data['mediaKey']}"
                        
                        response = await client.get(full_url, headers=headers)
                        
                        if response.status_code == 200:
                            audio_content = response.content
                            logger.info(f"🎤 Áudio baixado via directPath, tamanho: {len(audio_content)} bytes")
                        else:
                            logger.error(f"❌ Erro ao baixar áudio via directPath: {response.status_code}")
                except Exception as direct_err:
                    logger.error(f"❌ Erro ao processar áudio via directPath: {str(direct_err)}")
            
            # Verificação final se temos conteúdo para processar
            if audio_content is None or len(audio_content) < 100:  # Verificação de tamanho mínimo
                logger.error("❌ Nenhum conteúdo de áudio válido disponível para processamento")
                if audio_content is not None:
                    logger.debug(f"❌ Conteúdo de áudio muito pequeno: {len(audio_content)} bytes")
                return None
                
            try:
                # Criar arquivo temporário de entrada com o conteúdo extraído
                with tempfile.NamedTemporaryFile(delete=False, suffix=".opus") as temp_file:
                    temp_file.write(audio_content)
                    temp_path = temp_file.name
                    logger.info(f"🎤 Arquivo temporário de áudio original criado: {temp_path}")
                
                # Converter áudio para formato compatível com OpenAI
                converted_path = self.convert_audio(temp_path, "mp3")
                if not converted_path:
                    logger.warning("⚠️ Falha na conversão do áudio, tentando enviar o arquivo original...")
                    converted_path = temp_path
                
                # Fazer requisição à API da OpenAI com o arquivo convertido
                async with httpx.AsyncClient() as client:
                    with open(converted_path, "rb") as audio_file:
                        # Usar o tipo correto após a conversão
                        file_mimetype = "audio/mp3" if converted_path.endswith(".mp3") else "audio/ogg"
                        
                        logger.info(f"🎤 Enviando áudio para transcrição (formato: {file_mimetype})")
                        
                        stt_response = await client.post(
                            "https://api.openai.com/v1/audio/transcriptions",
                            headers={"Authorization": f"Bearer {os.getenv('OPENAI_API_KEY')}"},
                            files={"file": ("audio.mp3", audio_file, file_mimetype)},
                            data={"model": "whisper-1"}
                        )
                        logger.info(f"🎤 Status code da resposta: {stt_response.status_code}")
                        logger.debug(f"🎤 Resposta da API: {stt_response.text}")
                        
                        if stt_response.status_code == 200:
                            text = stt_response.json().get("text", "")
                            logger.info(f"🎤 Transcrição concluída: '{text}'")
                            return text
                        else:
                            logger.error(f"❌ Erro na transcrição: {stt_response.text}")
                            return None
            except Exception as e:
                logger.error(f"❌ Erro no processamento do áudio: {str(e)}")
                logger.exception("Stacktrace do erro:")
                return None
            finally:
                # Limpar arquivos temporários
                for path in [temp_path, converted_path]:
                    if path and os.path.exists(path):
                        try:
                            os.unlink(path)
                            logger.debug(f"🧹 Arquivo temporário removido: {path}")
                        except Exception as clean_err:
                            logger.warning(f"⚠️ Erro ao remover arquivo temporário {path}: {str(clean_err)}")
                
        except Exception as e:
            logger.error(f"❌ Erro geral na conversão de áudio para texto: {str(e)}")
            logger.exception("Stacktrace do erro:")
            return None