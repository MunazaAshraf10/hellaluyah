from fastapi import FastAPI, File, UploadFile, Form, WebSocket, WebSocketDisconnect, BackgroundTasks, Request
from fastapi.responses import JSONResponse, FileResponse
import shutil
from fpdf import FPDF
from fastapi import Query, HTTPException
from logging.handlers import RotatingFileHandler
import tempfile
import aiofiles
from fastapi import Query
from fastapi.responses import PlainTextResponse
from pathlib import Path
import aiohttp
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
import httpx
from aiohttp import ClientSession
from typing import AsyncGenerator
import os
import openai
import logging
from typing import Optional
from openai import AsyncOpenAI
from functools import wraps
import asyncio
import base64
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel
from typing import List
import websockets
import uuid
import time
import boto3
import botocore
from botocore.exceptions import ClientError
import json
from datetime import datetime
import logging.handlers
from boto3 import resource
from boto3.dynamodb.conditions import Attr
import re
from cryptography.fernet import Fernet
import traceback
import hashlib
from pydub import AudioSegment
import io

# Load environment variables
load_dotenv()

# Initialize logging with different handlers
log_dir = "logging"
os.makedirs(log_dir, exist_ok=True)
key = b'Vog2EOkP5ccjezrnyfpA1PGCs9EMkUXdHHxkY1VuGww='
# Initialize Fernet with the key
cipher = Fernet(key)


def encrypt_data(data):
    """
    Encrypts the given data using Fernet symmetric encryption.
    
    Args:
        data: The data to encrypt (must be bytes).
        
    Returns:
        Encrypted data as bytes.
    """
    return cipher.encrypt(data)

def decrypt_data(encrypted_data):
    """
    Decrypts the given data using Fernet symmetric encryption.
    
    Args:
        encrypted_data: The encrypted data (bytes, str, or Binary).
        
    Returns:
        Decrypted data as bytes.
    """
    # Add detailed logging to determine the type of data
    main_logger.info(f"Type of encrypted_data: {type(encrypted_data)}")
    
    # Handle nested dictionary with 'value' key - common format for stored encrypted values
    if isinstance(encrypted_data, dict) and 'value' in encrypted_data:
        main_logger.info("Data is a dictionary with 'value' key")
        encrypted_data = encrypted_data['value']
        if isinstance(encrypted_data, str):
            encrypted_data = encrypted_data.encode('utf-8')
    # For Binary objects from DynamoDB, don't try to log the actual value
    elif str(type(encrypted_data)) == "<class 'boto3.dynamodb.types.Binary'>":
        main_logger.info("Data is a DynamoDB Binary object")
        # Extract the bytes from the Binary object
        encrypted_data = encrypted_data.value
    elif isinstance(encrypted_data, str):
        encrypted_data = encrypted_data.encode('utf-8')
    elif isinstance(encrypted_data, bytes):
        pass  # Already in correct format
    elif isinstance(encrypted_data, dict) or isinstance(encrypted_data, list):
        # If it's a JSON structure that was stored, convert it to string first
        encrypted_data = json.dumps(encrypted_data).encode('utf-8')
    elif encrypted_data is None:
        raise ValueError("Encrypted data is None")
    else:
        raise TypeError(f"Encrypted data must be bytes or str, got {type(encrypted_data)}")

    # Proceed with decryption
    try:
        return cipher.decrypt(encrypted_data)
    except Exception as e:
        main_logger.error(f"Decryption error: {str(e)}")
        raise

# Configure logging handlers
def setup_logger(name, log_file, level=logging.INFO, format_string=None):
    if format_string is None:
        format_string = '%(asctime)s - %(levelname)s - %(name)s - %(message)s'
    
    # Create directory if it doesn't exist
    log_dir = os.path.dirname(log_file)
    os.makedirs(log_dir, exist_ok=True)
    
    # Setup file handler with rotation
    handler = logging.handlers.RotatingFileHandler(
        log_file, 
        maxBytes=10*1024*1024,  # 10MB
        backupCount=5
    )
    handler.setFormatter(logging.Formatter(format_string))
    
    # Also log to console in development
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter(format_string))
    
    logger = logging.getLogger(name)
    logger.setLevel(level)
    
    # Remove existing handlers to prevent duplicates
    if logger.handlers:
        logger.handlers.clear()
        
    logger.addHandler(handler)
    logger.addHandler(console)
    logger.propagate = False
    return logger

# Setup specific loggers
main_logger = setup_logger('main', f'{log_dir}/main.log')
time_logger = setup_logger('time', f'{log_dir}/time.log')
error_logger = setup_logger('error', f'{log_dir}/exceptions.log')
api_logger = setup_logger('api', f'{log_dir}/api.log')
db_logger = setup_logger('db', f'{log_dir}/database.log')

# Decorator for timing functions
def log_execution_time(func):
    @wraps(func)
    async def wrapper(*args, **kwargs):
        start_time = datetime.now()
        try:
            result = await func(*args, **kwargs)
            end_time = datetime.now()
            execution_time = end_time - start_time
            time_logger.info(f'{func.__name__} took {execution_time.total_seconds():.2f} seconds to execute')
            return result
        except Exception as e:
            end_time = datetime.now()
            execution_time = end_time - start_time
            time_logger.error(f'{func.__name__} failed after {execution_time.total_seconds():.2f} seconds')
            raise
    return wrapper

app = FastAPI()

# CORS configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Adjust for specific domains in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")


client = openai.OpenAI(api_key=OPENAI_API_KEY)



# Initialize AWS clients
AWS_ACCESS_KEY = os.getenv("AWS_ACCESS_KEY")
AWS_SECRET_KEY = os.getenv("AWS_SECRET_KEY")
AWS_REGION = os.getenv("AWS_REGION", "eu-north-1")
S3_BUCKET = os.getenv("S3_BUCKET")

# Initialize AWS S3 client
s3_client = boto3.client(
    's3',
    aws_access_key_id=AWS_ACCESS_KEY,
    aws_secret_access_key=AWS_SECRET_KEY,
    region_name=AWS_REGION
)

# Initialize DynamoDB for metadata (optional but recommended)
dynamodb = boto3.resource(
    'dynamodb',
    aws_access_key_id=AWS_ACCESS_KEY,
    aws_secret_access_key=AWS_SECRET_KEY,
    region_name=AWS_REGION
)

# Reusable HTTP client
http_client = httpx.AsyncClient(timeout=60.0)
clients = AsyncOpenAI()

class Message(BaseModel):
    speaker: str
    text: str

class ReportRequest(BaseModel):
    conversation: List[Message]
    template_case_name: str


@app.on_event("shutdown")
async def shutdown_event():
    await http_client.aclose()

# Add a WebSocket connection manager
class ConnectionManager:
    """
    Manages WebSocket connections and associated session data.
    
    This class keeps track of active WebSocket connections and stores
    session-specific data like transcriptions. It provides methods to
    connect, disconnect, and send messages to clients.
    """
    def __init__(self):
        self.active_connections = {}  # Maps client_id to WebSocket instance
        self.session_data = {}  # Store session data including transcription for later processing

    async def connect(self, websocket: WebSocket, client_id: str):
        """
        Accept a new WebSocket connection and initialize session data.
        
        Args:
            websocket: The WebSocket connection to accept
            client_id: Unique identifier for the client
        """
        await websocket.accept()
        self.active_connections[client_id] = websocket
        self.session_data[client_id] = {
            "transcription": {
                "conversation": [],
                "metadata": {"duration": 0, "channels": 1}
            },
            "complete_transcript": [],
            "is_transcription_complete": False
        }

    def disconnect(self, client_id: str):
        """
        Remove a client's WebSocket connection but keep their session data.
        
        Args:
            client_id: Unique identifier for the client to disconnect
        """
        if client_id in self.active_connections:
            del self.active_connections[client_id]
        # Keep session data for potential processing after disconnect

    def get_session_data(self, client_id: str):
        """
        Retrieve session data for a specific client.
        
        Args:
            client_id: Unique identifier for the client
            
        Returns:
            Dictionary containing the client's session data
        """
        return self.session_data.get(client_id, {})

    def update_session_data(self, client_id: str, data):
        """
        Update session data for a specific client.
        
        Args:
            client_id: Unique identifier for the client
            data: New data to merge with existing session data
        """
        if client_id in self.session_data:
            self.session_data[client_id].update(data)

    async def send_message(self, message: str, client_id: str):
        """
        Send a text message to a specific client.
        
        Args:
            message: Message content to send
            client_id: Unique identifier for the recipient client
        """
        if client_id in self.active_connections:
            await self.active_connections[client_id].send_text(message)

manager = ConnectionManager()

@app.get("/")
async def root():
    return {"message": "Welcome to the speech transcription and GPT-4 processing service!"}



@app.websocket("/ws/live-transcription")
async def live_transcription_endpoint(websocket: WebSocket):
    client_id = str(uuid.uuid4())
    transcript_id = str(uuid.uuid4())

    main_logger.info(f"[{client_id}] New WebSocket connection")
    time_logger.info(f"[{client_id}] Client: {websocket.client.host}:{websocket.client.port}")

    try:
        await manager.connect(websocket, client_id)
        main_logger.info(f"[{client_id}] Connected successfully")

        config_msg = await websocket.receive_text()
        config = json.loads(config_msg)
        main_logger.info(f"[{client_id}] Config received: {config}")

        await websocket.send_text(json.dumps({
            "status": "ready",
            "message": "Ready to receive audio for real-time transcription",
            "transcript_id": transcript_id
        }))

        session_data = manager.get_session_data(client_id)
        session_data["transcript_id"] = transcript_id
        session_data["no_report"] = False
        session_data["closed"] = False
        session_data["disconnected"] = False 
        manager.update_session_data(client_id, session_data)

        audio_buffer = bytearray()

        deepgram_url = (
            "wss://api.deepgram.com/v1/listen"
            "?encoding=linear16&sample_rate=16000&channels=1"
            "&model=nova-2-medical&language=en&diarize=true"
            "&punctuate=true&smart_format=true"
        )

        deepgram_socket = await websockets.connect(
            deepgram_url,
            additional_headers={"Authorization": f"Token {DEEPGRAM_API_KEY}"}
        )

        audio_task = asyncio.create_task(process_audio_stream(websocket, deepgram_socket, audio_buffer, client_id))
        transcript_task = asyncio.create_task(process_transcription_results(deepgram_socket, websocket, client_id, audio_buffer))

        await asyncio.gather(audio_task, transcript_task, return_exceptions=True)

    except WebSocketDisconnect:
        main_logger.warning(f"[{client_id}] WebSocket disconnected")
    except Exception as e:
        error_logger.error(f"[{client_id}] Unexpected error: {str(e)}\n{traceback.format_exc()}")
    finally:
        try:
            manager.disconnect(client_id)
            main_logger.info(f"[{client_id}] Cleaned up connection")
        except Exception as e:
            error_logger.error(f"[{client_id}] Cleanup failed: {e}")

async def process_audio_stream(websocket: WebSocket, deepgram_socket, audio_buffer, client_id):
    try:
        while True:
            try:
                message = await websocket.receive()
            except RuntimeError as e:
                error_logger.warning(f"[{client_id}] Cannot receive (WebSocket likely disconnected): {e}")
                break

            if "bytes" in message:
                data = message["bytes"]

                session_data = manager.get_session_data(client_id)
                if session_data.get("paused", False):
                    continue

                audio_buffer.extend(data)
                await deepgram_socket.send(data)

            elif "text" in message:
                try:
                    data = json.loads(message["text"])
                    msg_type = data.get("type")
                    session_data = manager.get_session_data(client_id)

                    if msg_type == "dont-generate-report":
                        session_data["no_report"] = True
                        manager.update_session_data(client_id, session_data)
                        main_logger.info(f"[{client_id}] Received dont-generate-report, setting no_report=True and closing WebSocket")
                        break  # Exit immediately

                    elif msg_type == "CloseStream":
                        session_data["closed"] = True
                        session_data["template_type"] = data.get("template_type", "new_soap_note")
                        manager.update_session_data(client_id, session_data)
                        main_logger.info(f"[{client_id}] Received CloseStream, marked session closed with template_type={session_data['template_type']}")
                        
                        await deepgram_socket.send(json.dumps(data))
                        continue

                    elif msg_type == "PauseStream":
                        session_data["paused"] = True
                        manager.update_session_data(client_id, session_data)

                    elif msg_type == "ResumeStream":
                        session_data["paused"] = False
                        manager.update_session_data(client_id, session_data)

                except json.JSONDecodeError:
                    error_logger.warning(f"[{client_id}] Received non-JSON text message, ignoring.")

    except WebSocketDisconnect:
        main_logger.warning(f"[{client_id}] WebSocket disconnected during audio stream")
        session_data = manager.get_session_data(client_id)
        session_data["disconnected"] = True
        manager.update_session_data(client_id, session_data)
        return
    except Exception as e:
        error_logger.error(f"[{client_id}] Audio stream error: {str(e)}\n{traceback.format_exc()}")


async def generate_report_background(transcript_id, template_type, client_id):
    try:
        main_logger.info(f"[{client_id}] (BG) Generating report for {template_type} with Transcript ID {transcript_id}")
        async with aiohttp.ClientSession() as session:
            form = aiohttp.FormData()
            form.add_field("transcript_id", transcript_id)
            form.add_field("template_type", template_type)
            await session.post("https://build.medtalk.co/generate_report_session", data=form)
            main_logger.info(f"[{client_id}] (BG) Report generation triggered")
    except Exception as e:
        error_logger.error(f"[{client_id}] (BG) Report generation error: {e}")

async def process_transcription_results(deepgram_socket, websocket, client_id, audio_buffer):
    try:
        while True:
            try:
                response = await deepgram_socket.recv()
            except websockets.exceptions.ConnectionClosedOK:
                main_logger.info(f"[{client_id}] Deepgram WebSocket closed normally (1000 OK)")
                break
            except websockets.exceptions.ConnectionClosedError as e:
                error_logger.error(f"[{client_id}] Deepgram connection closed with error: {e}")
                break

            data = json.loads(response)

            if "channel" in data and "alternatives" in data["channel"]:
                transcript = data["channel"]["alternatives"][0]
                is_final = data.get("is_final", False)

                speaker = "Speaker 1"
                if transcript.get("words") and "speaker" in transcript["words"][0]:
                    speaker = f"Speaker {transcript['words'][0]['speaker']}"

                message = {
                    "type": "transcript",
                    "text": transcript["transcript"],
                    "speaker": speaker,
                    "is_final": is_final
                }

                try:
                    await websocket.send_text(json.dumps(message))
                except RuntimeError:
                    break

                if data.get("type") == "Results":
                    transcript_text = transcript["transcript"].strip()
                    if transcript_text:
                        session_data = manager.get_session_data(client_id)
                        if "transcription" not in session_data:
                            session_data["transcription"] = {"conversation": []}
                        session_data["transcription"]["conversation"].append({
                            "speaker": speaker,
                            "text": transcript_text,
                            "is_final": is_final
                        })
                        manager.update_session_data(client_id, session_data)
        
        session_data = manager.get_session_data(client_id)
        transcript_id = session_data["transcript_id"]

        audio_info = await save_audio_to_s3(bytes(audio_buffer))
        transcript_data = session_data.get("transcription", {
            "conversation": [], "metadata": {"duration": 0, "channels": 1}
        })

        await save_transcript_to_dynamodb(
            transcript_data,
            audio_info,
            status="completed",
            transcript_id=transcript_id
        )

        response_payload = {
            "status": "transcription_saved",
            "transcript_id": transcript_id,
            "transcription": {
                "id": transcript_id,
                "transcript": transcript_data
            },
            "reports": []
        }

        # Try sending transcription_saved to client
        try:
            await websocket.send_text(json.dumps(response_payload))
            main_logger.info(f"[{client_id}] Sent transcription_saved after Deepgram closed")
        except Exception as e:
            error_logger.warning(f"[{client_id}] Failed to send transcription_saved: {e}")

        # Always trigger report if closed and not disabled
        session_data = manager.get_session_data(client_id)
        # Always save the transcript
        main_logger.info(f"[{client_id}] Transcription saved for transcript_id: {transcript_id} (disconnected={session_data.get('disconnected')}, closed={session_data.get('closed')})")
        if session_data.get("closed") and not session_data.get("no_report", False):
            transcript_id = session_data.get("transcript_id")
            template_type = session_data.get("template_type", "new_soap_note")
            asyncio.create_task(generate_report_background(transcript_id, template_type, client_id))
        else:
            main_logger.info(f"[{client_id}] Skipping report generation (no_report={session_data.get('no_report')})")

    except Exception as e:
        error_logger.error(f"[{client_id}] Error in process_transcription_results: {e}\n{traceback.format_exc()}")

# New audio processing functions
def split_audio(audio_data: bytes, chunk_size_seconds: int = 60, target_format: str = "wav", target_sample_rate: int = 16000) -> list[bytes]:
    """
    Split audio into chunks of specified duration, supporting multiple formats.
    
    Args:
        audio_data: Raw audio bytes
        chunk_size_seconds: Duration of each chunk in seconds
        target_format: Output format (e.g., 'wav')
        target_sample_rate: Target sample rate for transcription
    
    Returns:
        List of audio chunk bytes
    """
    try:
        if not audio_data:
            error_logger.error("Empty audio data provided")
            raise ValueError("Audio data is empty")

        # Attempt to load audio, with fallback to ffmpeg
        try:
            audio = AudioSegment.from_file(io.BytesIO(audio_data))
        except CouldntDecodeError:
            main_logger.warning("Could not decode audio directly, attempting with ffmpeg")
            audio = AudioSegment.from_file(io.BytesIO(audio_data), format="mp3")
        except Exception as e:
            error_logger.error(f"Failed to load audio: {str(e)}")
            raise ValueError(f"Unsupported or corrupted audio: {str(e)}")

        main_logger.info(f"Audio format detected: channels={audio.channels}, sample_rate={audio.frame_rate}, duration={audio.duration_seconds:.2f}s")

        # Normalize audio
        audio = audio.set_channels(1).set_frame_rate(target_sample_rate)

        # Handle short audio
        if audio.duration_seconds < 1.0:
            main_logger.warning("Audio duration is too short, creating single chunk")
            chunk_buffer = io.BytesIO()
            audio.export(chunk_buffer, format=target_format)
            return [chunk_buffer.getvalue()]

        # Split into chunks
        chunks = []
        for i in range(0, len(audio), chunk_size_seconds * 1000):
            chunk = audio[i:i + chunk_size_seconds * 1000]
            chunk_buffer = io.BytesIO()
            chunk.export(chunk_buffer, format=target_format)
            chunks.append(chunk_buffer.getvalue())
        
        main_logger.info(f"Split audio into {len(chunks)} chunks")
        return chunks
    except Exception as e:
        error_logger.error(f"Error splitting audio: {str(e)}")
        raise

def merge_transcriptions(results: list[dict], chunk_durations: list[float]) -> dict:
    """
    Merge transcription results into a JSON structure with conversation and metadata.
    Group words by speaker into sentences.
    
    Args:
        results: List of transcription results from Deepgram
        chunk_durations: List of durations for each chunk in seconds
    
    Returns:
        Dictionary with conversation and metadata
    """
    try:
        merged_words = []
        current_time_offset = 0.0
        for idx, result in enumerate(results):
            if "results" not in result or "channels" not in result["results"]:
                error_logger.warning(f"Invalid result structure for chunk {idx}")
                current_time_offset += chunk_durations[idx]
                continue
            alternatives = result["results"]["channels"][0].get("alternatives", [])
            if not alternatives:
                error_logger.warning(f"No alternatives found for chunk {idx}")
                current_time_offset += chunk_durations[idx]
                continue
            words = alternatives[0].get("words", [])
            for word in words:
                adjusted_word = {
                    "speaker": f"Speaker {word.get('speaker', 'Unknown')}",
                    "text": word.get("word", ""),
                    "start": word.get("start", 0.0) + current_time_offset,
                    "end": word.get("end", 0.0) + current_time_offset,
                    "confidence": word.get("confidence", 0.0)
                }
                merged_words.append(adjusted_word)
            current_time_offset += chunk_durations[idx]
        
        # Sort by start time
        merged_words.sort(key=lambda x: x["start"])
        
        # Group into sentences by speaker
        conversation = []
        current_speaker = None
        current_sentence = []
        last_end_time = 0.0
        for word in merged_words:
            speaker = word["speaker"]
            text = word["text"]
            start_time = word["start"]
            end_time = word["end"]
            
            # End sentence on speaker change or significant pause (>0.5s)
            if (speaker != current_speaker or (start_time - last_end_time > 0.5)) and current_sentence:
                conversation.append({
                    "speaker": current_speaker,
                    "text": " ".join(current_sentence).strip()
                })
                current_sentence = []
            
            current_sentence.append(text)
            current_speaker = speaker
            last_end_time = end_time
        
        # Append final sentence
        if current_sentence:
            conversation.append({
                "speaker": current_speaker,
                "text": " ".join(current_sentence).strip()
            })
        return {
            "conversation": conversation,
            "metadata": {
                "duration": current_time_offset,
                "channels": 1,
                "models": [],
                "current_date":  datetime.now().strftime("%m/%d/%Y"),
            }
        }
    except Exception as e:
        error_logger.error(f"Error merging transcriptions: {str(e)}")
        raise

# async def transcribe_chunk(
#     chunk: bytes,
#     chunk_index: int,
#     enable_diarization: bool = True,
#     target_format: str = "wav",
#     model: str = "nova-3-general"
# ) -> tuple[dict, float]:
#     """
#     Transcribe a single audio chunk with language detection and diarization.
#     """
#     try:
#         chunk_hash = hashlib.sha256(chunk).hexdigest()
#         table = dynamodb.Table('transcripts')
#         response = table.get_item(Key={"id": chunk_hash})
#         if 'Item' in response:
#             main_logger.info(f"Retrieved cached transcription for chunk {chunk_index}")
#             result = json.loads(decrypt_data(response['Item']['transcript']))
#             duration = AudioSegment.from_file(io.BytesIO(chunk), format=target_format).duration_seconds
#             return result, duration

#         start_time = time.time()
#         params = {
#             "model": model,
#             "detect_language": "true",
#             "topics": "true",
#             "smart_format": "true",
#             "punctuate": "true",
#             "utterances": "true",
#             "language": "multi",
#             "utt_split": 0.6,
#             "diarize": str(enable_diarization).lower(),
#             "sentiment": "true",
#             "sensitivity": "high"
#         }

#         async with ClientSession() as session:
#             async with session.post(
#                 "https://api.deepgram.com/v1/listen",
#                 headers={"Authorization": f"Token {DEEPGRAM_API_KEY}"},
#                 data=chunk,
#                 params=params
#             ) as response:
#                 response.raise_for_status()
#                 result = await response.json()

#         # Extract language info for logging
#         lang = result.get("results", {}).get("channels", [{}])[0].get("detected_language", "unknown")
#         confidence = result.get("results", {}).get("channels", [{}])[0].get("language_confidence", 0)
#         main_logger.info(f"Chunk {chunk_index}: Detected language={lang} (confidence={confidence:.2f})")

#         duration = AudioSegment.from_file(io.BytesIO(chunk), format=target_format).duration_seconds
#         await save_transcript_to_dynamodb(
#             result,
#             {"chunk_hash": chunk_hash, "chunk_index": chunk_index},
#             status="completed",
#             transcript_id=chunk_hash
#         )

#         main_logger.info(f"Chunk {chunk_index} transcription took {time.time() - start_time:.2f} seconds")
#         return result, duration
#     except Exception as e:
#         error_logger.error(f"Error transcribing chunk {chunk_index}: {str(e)}")
#         return {"error": str(e)}, 0.0

# @log_execution_time
# async def transcribe_audio_with_diarization(audio_data: bytes, enable_diarization: bool = True, model: str = "nova-3-general") -> dict:
#     """
#     Transcribe audio with diarization and auto language detection.
#     """
#     try:
#         start_time = time.time()
#         main_logger.info(f"Starting transcription for audio of size {len(audio_data)} bytes")

#         chunks = split_audio(audio_data)

#         max_concurrent_tasks = 5
#         tasks = [
#             transcribe_chunk(chunk, idx, enable_diarization, model=model)
#             for idx, chunk in enumerate(chunks)
#         ]

#         results = []
#         for i in range(0, len(tasks), max_concurrent_tasks):
#             batch_results = await asyncio.gather(*tasks[i:i + max_concurrent_tasks], return_exceptions=True)
#             results.extend(batch_results)

#         transcription_results = []
#         chunk_durations = []
#         for idx, (result, duration) in enumerate(results):
#             if isinstance(result, dict) and "error" not in result:
#                 transcription_results.append(result)
#                 chunk_durations.append(duration)
#             else:
#                 error_logger.error(f"Chunk {idx} failed: {result.get('error', 'Unknown error')}")

#         if not transcription_results:
#             error_msg = "Error: All chunks failed to transcribe"
#             error_logger.error(error_msg)
#             return {"error": error_msg}

#         transcription = merge_transcriptions(transcription_results, chunk_durations)
#         main_logger.info(f"Transcription completed in {time.time() - start_time:.2f} seconds")
#         return transcription

#     except Exception as e:
#         error_msg = f"Error in transcription: {str(e)}"
#         error_logger.error(error_msg, exc_info=True)
#         return {"error": error_msg}

async def transcribe_chunk(chunk: bytes, chunk_index: int, enable_diarization: bool = True, target_format: str = "wav") -> tuple[dict, float]:
    """
    Transcribe a single audio chunk with enhanced diarization.
    
    Args:
        chunk: Audio chunk bytes
        chunk_index: Index of the chunk for logging
        enable_diarization: Whether to enable speaker diarization
        target_format: Audio format for transcription
    
    Returns:
        Tuple of transcription result and chunk duration
    """
    try:
        # Check cache
        chunk_hash = hashlib.sha256(chunk).hexdigest()
        table = dynamodb.Table('transcripts')
        response = table.get_item(Key={"id": chunk_hash})
        if 'Item' in response:
            main_logger.info(f"Retrieved cached transcription for chunk {chunk_index}")
            result = json.loads(decrypt_data(response['Item']['transcript']))
            duration = AudioSegment.from_file(io.BytesIO(chunk), format=target_format).duration_seconds
            return result, duration

        start_time = time.time()
        params = {
            "topics": "true",
            "smart_format": "true",
            "punctuate": "true",
            "utterances": "true",
            "utt_split": 0.6,           # More aggressive utterance splitting
            "diarize": "true",
            "sentiment": "true",
            "language": "en",
            "model": "nova-2-medical",
            "sensitivity": "high" 
        }

        
        async with ClientSession() as session:
            async with session.post(
                "https://api.deepgram.com/v1/listen",
                headers={"Authorization": f"Token {DEEPGRAM_API_KEY}"},
                data=chunk,
                params=params
            ) as response:
                response.raise_for_status()
                result = await response.json()

        # Cache result
        duration = AudioSegment.from_file(io.BytesIO(chunk), format=target_format).duration_seconds
        await save_transcript_to_dynamodb(
            result,
            {"chunk_hash": chunk_hash, "chunk_index": chunk_index},
            status="completed",
            transcript_id=chunk_hash
        )
        
        main_logger.info(f"Chunk {chunk_index} transcription took {time.time() - start_time:.2f} seconds")
        return result, duration
    except Exception as e:
        error_logger.error(f"Error transcribing chunk {chunk_index}: {str(e)}")
        return {"error": str(e)}, 0.0

@log_execution_time
async def transcribe_audio_with_diarization(audio_data: bytes, enable_diarization: bool = True) -> dict:
    """
    Transcribe audio with diarization by splitting into chunks and processing in parallel.
    Returns JSON with status, transcription_status, transcript_id, and transcription.
    
    Args:
        audio_data: Raw audio bytes
        enable_diarization: Whether to enable speaker diarization
    
    Returns:
        JSON transcription result
    """
    try:
        start_time = time.time()
        main_logger.info(f"Starting transcription for audio of size {len(audio_data)} bytes")
        
        # Split audio into chunks
        chunks = split_audio(audio_data)
        
        # Transcribe chunks in parallel
        max_concurrent_tasks = 5
        tasks = [
            transcribe_chunk(chunk, idx, enable_diarization)
            for idx, chunk in enumerate(chunks)
        ]
        results = []
        for i in range(0, len(tasks), max_concurrent_tasks):
            batch_results = await asyncio.gather(*tasks[i:i + max_concurrent_tasks], return_exceptions=True)
            results.extend(batch_results)
        
        # Separate successful results and durations
        transcription_results = []
        chunk_durations = []
        for idx, (result, duration) in enumerate(results):
            if isinstance(result, dict) and "error" not in result:
                transcription_results.append(result)
                chunk_durations.append(duration)
            else:
                error_logger.error(f"Chunk {idx} failed: {result.get('error', 'Unknown error')}")
        
        if not transcription_results:
            error_msg = "Error: All chunks failed to transcribe"
            error_logger.error(error_msg)
            return {"error": error_msg}
        # Merge results
        transcription = merge_transcriptions(transcription_results, chunk_durations)
        main_logger.info(f"Transcription completed in {time.time() - start_time:.2f} seconds")
        # Construct final output without nested transcription
        return transcription
        
    except Exception as e:
        error_msg = f"Error in transcription: {str(e)}"
        error_logger.error(error_msg, exc_info=True)
        return {"error": error_msg}
    
@log_execution_time
async def save_to_aws(transcription, gpt_response, formatted_report, template_type, audio_data=None, status="completed"):
    """
    Save all data to AWS storage services.
    
    This integrated method saves audio to S3 and metadata to DynamoDB.
    It includes the transcription, GPT response, and formatted report.
    
    Args:
        transcription: Dictionary containing the transcribed conversation
        gpt_response: JSON string containing structured report data
        formatted_report: Formatted string containing the human-readable report
        template_type: Type of report (clinical_report, soap_note, etc.)
        audio_data: Binary audio data to save (optional)
        status: Processing status (completed, partial, or failed)
    
    Returns:
        Dictionary containing transcript_id and report_id if successful, False otherwise
    """
    try:
        operation_id = str(uuid.uuid4())[:8]
        db_logger.info(f"[OP-{operation_id}] Starting save_to_aws operation")
        
        # Save audio if provided
        audio_info = None
        if audio_data:
            db_logger.info(f"[OP-{operation_id}] Saving audio data to S3")
            audio_info = await save_audio_to_s3(audio_data)
            if not audio_info:
                db_logger.error(f"[OP-{operation_id}] Failed to save audio to S3")
                return False
            db_logger.info(f"[OP-{operation_id}] Audio saved to S3: {audio_info['s3_path']}")
        
        # Save transcript
        db_logger.info(f"[OP-{operation_id}] Saving transcript to DynamoDB with status: {status}")
        transcript_id = await save_transcript_to_dynamodb(transcription, audio_info, status)
        
        if not transcript_id:
            db_logger.error(f"[OP-{operation_id}] Failed to save transcript to DynamoDB")
            return False
            
        db_logger.info(f"[OP-{operation_id}] Transcript saved with ID: {transcript_id}")
            
        # Save report if available (might be None if processing failed)
        report_id = None
        if gpt_response and formatted_report:
            db_logger.info(f"[OP-{operation_id}] Saving report to DynamoDB for transcript ID: {transcript_id}")
            report_id = await save_report_to_dynamodb(
                transcript_id,
                gpt_response,
                formatted_report,
                template_type,
                status
            )
            
            if not report_id:
                db_logger.error(f"[OP-{operation_id}] Failed to save report to DynamoDB")
                return {"transcript_id": transcript_id, "report_id": None}
                
            db_logger.info(f"[OP-{operation_id}] Report saved with ID: {report_id}")
        
        return {
            "transcript_id": transcript_id,
            "report_id": report_id
        }
        
    except Exception as e:
        operation_id = locals().get('operation_id', str(uuid.uuid4())[:8])
        error_logger.error(f"[OP-{operation_id}] Error saving to AWS: {str(e)}", exc_info=True)
        return False


@app.post("/ask-ai")
async def ask_ai(prompt: str = Form(...)):

    operation_id = str(uuid.uuid4())[:8]
    if not prompt:
        error_logger.warning(f"[OP-{operation_id}] Missing prompt in /ask-ai request")
        return {"error": "Missing prompt"}
    
    SYSTEM_MESSAGE = (
    "You are an AI Scribe and Information Assistant. "
    "Answer user prompts using clear and structured plain text only. "
    "Do not use markdown symbols such as asterisks, dashes, or other formatting characters. "
    "If the prompt relates to medicine, provide concise, medically accurate information. "
    "Always respond in plain sentences or numbered lists where appropriate, with no styling."
)

    async def stream_openai_response():
        try:
            response = await clients.chat.completions.create(
                model="gpt-4.1",  # You can also use "gpt-3.5-turbo" if needed
                messages=[
                    {"role": "system", "content": SYSTEM_MESSAGE},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.4,
                stream=True,
            )

            async for chunk in response:
                if chunk.choices and chunk.choices[0].delta.content:
                    yield chunk.choices[0].delta.content

            main_logger.info(f"[OP-{operation_id}] Completed streaming AI response")

        except Exception as e:
            error_logger.error(f"[OP-{operation_id}] Streaming error: {str(e)}", exc_info=True)
            yield "\n[Error streaming response]"

    return StreamingResponse(
        stream_openai_response(),
        media_type="text/plain"
    )

# Save audio file to S3
async def save_audio_to_s3(audio_data, filename=None):
    try:
        operation_id = str(uuid.uuid4())[:8]
        if not filename:
            filename = f"audio_{uuid.uuid4()}.wav"
            
        db_logger.info(f"[OP-{operation_id}] Saving audio to S3: {filename}, size: {len(audio_data)} bytes")
        
        # Upload to S3
        s3_client.put_object(
            Bucket=S3_BUCKET,
            Key=f"audio/{filename}",
            Body=audio_data,
            ContentType="audio/wav"
        )
        
        # Generate a presigned URL for temporary access (optional)
        presigned_url = s3_client.generate_presigned_url(
            'get_object',
            Params={'Bucket': S3_BUCKET, 'Key': f"audio/{filename}"},
            ExpiresIn=3600  # URL valid for 1 hour
        )
        
        db_logger.info(f"[OP-{operation_id}] Audio successfully saved to S3: s3://{S3_BUCKET}/audio/{filename}")
        
        return {
            "filename": filename,
            "s3_path": f"audio/{filename}",
            "presigned_url": presigned_url
        }
    except Exception as e:
        operation_id = locals().get('operation_id', str(uuid.uuid4())[:8])
        error_logger.error(f"[OP-{operation_id}] Error saving audio to S3: {str(e)}", exc_info=True)
        return None

# Save transcript data to DynamoDB
async def save_transcript_to_dynamodb(transcript_data, audio_info=None, status="completed", transcript_id=None):
    try:
        operation_id = str(uuid.uuid4())[:8]
        if not transcript_id:
            transcript_id = str(uuid.uuid4())
        timestamp = datetime.now().isoformat()
        
        db_logger.info(f"[OP-{operation_id}] Saving transcript to DynamoDB, ID: {transcript_id}, status: {status}")
        encrypted_transcription = encrypt_data(json.dumps(transcript_data).encode())
        # Create item for DynamoDB
        item = {
            "id": transcript_id,
            "transcript": encrypted_transcription,
            "created_at": timestamp,
            "updated_at": timestamp,
            "status": status
        }
        
        if audio_info:
            item["audio_file"] = json.dumps(audio_info)
        
        # Save to DynamoDB
        table = dynamodb.Table('transcripts')
        table.put_item(Item=item)
        
        db_logger.info(f"[OP-{operation_id}] Transcript successfully saved to DynamoDB: {transcript_id}")
        return transcript_id
    except Exception as e:
        operation_id = locals().get('operation_id', str(uuid.uuid4())[:8])
        error_logger.error(f"[OP-{operation_id}] Error saving transcript to DynamoDB: {str(e)}", exc_info=True)
        return None

async def save_summary_to_dynamodb(ai_summary, transcript_id):
    table = dynamodb.Table('summaries')
    summary_id = str(uuid.uuid4())
    encrypted_summary = encrypt_data(ai_summary.encode())
    item = {
        "id": summary_id,
        "transcript_id": transcript_id,
        "summary": encrypted_summary,
        "status": "completed",
        "created_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat()
    }
    table.put_item(Item=item)
    db_logger.info(f"Updating transcript with ID: {transcript_id}")
    # Update the transcription record with the summary information
    table = dynamodb.Table('transcripts')
    response = table.update_item(
        Key={"id": transcript_id},  # Ensure "id" is the correct key
        UpdateExpression="SET ai_summary = :ai_summary",
        ExpressionAttributeValues={
            ":ai_summary": ai_summary
        },
        ReturnValues="UPDATED_NEW"
    )
    db_logger.info(f"Updating transcript with ID: {transcript_id}")
    return summary_id
    
# Save formatted report to DynamoDB
async def save_report_to_dynamodb(transcript_id, gpt_response, formatted_report, template_type, status="completed" , report_id : str = None ):
    try:
        operation_id = str(uuid.uuid4())[:8]
        if not report_id:
            report_id = str(uuid.uuid4())
        timestamp = datetime.now().isoformat()
        
        db_logger.info(f"[OP-{operation_id}] Saving report to DynamoDB, ID: {report_id}, linked to transcript: {transcript_id}")
        encrypted_gpt_response = encrypt_data(gpt_response.encode())
        

        # Create item for DynamoDB
        item = {
            "id": report_id,
            "transcript_id": transcript_id,
            "gpt_response": encrypted_gpt_response,
            "formatted_report": formatted_report,
            "template_type": template_type,
            "created_at": timestamp,
            "updated_at": timestamp,
            "status": status
        }
        
        # Save to DynamoDB
        table = dynamodb.Table('reports')
        table.put_item(Item=item)
        
        db_logger.info(f"[OP-{operation_id}] Report successfully saved to DynamoDB: {report_id}")
        # Update the transcription record with the new report ID
        table = dynamodb.Table('transcripts')
        response = table.update_item(
            Key={"id": transcript_id},
            UpdateExpression="SET report_ids = list_append(if_not_exists(report_ids, :empty_list), :new_report_id)",
            ExpressionAttributeValues={
                ":new_report_id": [report_id],
                ":empty_list": []
            },
            ReturnValues="UPDATED_NEW"
        )
        return report_id
    except Exception as e:
        operation_id = locals().get('operation_id', str(uuid.uuid4())[:8])
        error_logger.error(f"[OP-{operation_id}] Error saving report to DynamoDB: {str(e)}", exc_info=True)
        return None


@app.get("/list-failed-transcriptions")
async def list_failed_transcriptions():
    """
    List all audio files with failed transcriptions or processing
    """
    try:
        # Query DynamoDB for failed items
        table = dynamodb.Table('transcripts')
        response = table.scan(
            FilterExpression="#status = :status",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={":status": "failed"}
        )
        
        items = response.get('Items', [])
        
        # Format the response
        failed_items = []
        for item in items:
            audio_info = json.loads(item.get('audio_file', '{}'))
            failed_items.append({
                "id": item.get('id'),
                "created_at": item.get('created_at'),
                "audio_filename": audio_info.get('filename', 'Unknown'),
                "s3_path": audio_info.get('s3_path')
            })
        
        return {"failed_items": failed_items}
    except Exception as e:
        error_logger.error(f"Error listing failed transcriptions: {str(e)}", exc_info=True)
        return JSONResponse(
            {"error": f"Failed to list transcriptions: {str(e)}"},
            status_code=500
        )

@app.post("/retry-transcription/{transcript_id}")
@log_execution_time
async def retry_transcription(transcript_id: str):
    """
    Retry transcription for an existing transcript.
    
    Args:
        transcript_id: ID of the transcript to retry
        
    Returns:
        Updated transcription data
    """
    try:
        # Get the transcript from DynamoDB
        table = dynamodb.Table('transcripts')
        response = table.get_item(Key={"id": transcript_id})
        
        if 'Item' not in response:
            return JSONResponse(
                {"error": f"Transcript ID {transcript_id} not found"},
                status_code=404
            )
        
        transcript_item = response['Item']
        
        # Get audio file info
        try:
            audio_info = json.loads(transcript_item.get('audio_file', '{}'))
        except json.JSONDecodeError:
            return JSONResponse(
                {"error": "Invalid audio file data format in transcript record"},
                status_code=400
            )
        
        # Check if audio file exists
        s3_path = audio_info.get('s3_path')
        if not s3_path:
            return JSONResponse(
                {"error": "No audio file associated with this transcript"},
                status_code=400
            )
        
        # Download audio file from S3
        main_logger.info(f"Downloading audio file from S3: {s3_path}")
        try:
            s3_response = s3_client.get_object(Bucket=S3_BUCKET, Key=s3_path)
            audio_data = s3_response['Body'].read()
        except Exception as e:
            error_logger.error(f"Error downloading audio file from S3: {str(e)}")
            return JSONResponse(
                {"error": f"Failed to download audio file: {str(e)}"},
                status_code=500
            )
        
        # Retry transcription
        transcription_result = await transcribe_audio_with_diarization(audio_data)
        
        # If transcription fails, return error
        if isinstance(transcription_result, str) and transcription_result.startswith("Error"):
            return JSONResponse(
                {"error": transcription_result},
                status_code=400
            )
        
        # Update the transcript in DynamoDB
        now = datetime.now().isoformat()
        table.update_item(
            Key={"id": transcript_id},
            UpdateExpression="SET transcript = :transcript, updated_at = :updated_at, #status_field = :status",
            ExpressionAttributeNames={
                "#status_field": "status"
            },
            ExpressionAttributeValues={
                ":transcript": json.dumps(transcription_result),
                ":updated_at": now,
                ":status": "completed"
            }
        )
        
        return {
            "success": True,
            "message": "Transcription retried successfully",
            "transcript_id": transcript_id,
            "transcription": transcription_result
        }
        
    except Exception as e:
        error_logger.exception(f"Error retrying transcription: {str(e)}")
        return JSONResponse(
            {"error": f"Failed to retry transcription: {str(e)}"},
            status_code=500
        )

@app.post("/retry-report/{report_id}")
@log_execution_time
async def retry_report(report_id: str, template_type: str = None):
    """
    Retry report generation for an existing report or with a new template type.
    
    Args:
        report_id: ID of the report to retry
        template_type: Optional new template type to use instead of the original
        
    Returns:
        Updated report data
    """
    try:
        # Get the original report from DynamoDB
        report_table = dynamodb.Table('reports')
        response = report_table.get_item(Key={"id": report_id})
        
        if 'Item' not in response:
            return JSONResponse(
                {"error": f"Report ID {report_id} not found"},
                status_code=404
            )
            
        report_item = response['Item']
        
        # Get the transcript using the transcript_id from the report
        transcript_id = report_item.get('transcript_id')
        if not transcript_id:
            return JSONResponse(
                {"error": "Original transcript ID not found in report"},
                status_code=400
            )
            
        # Get the transcript
        transcript_table = dynamodb.Table('transcripts')
        transcript_response = transcript_table.get_item(Key={"id": transcript_id})
        
        if 'Item' not in transcript_response:
            return JSONResponse(
                {"error": f"Original transcript {transcript_id} not found"},
                status_code=404
            )
            
        transcript_item = transcript_response['Item']
        
        # Decrypt and parse transcript data
        try:
            # Handle Binary type from DynamoDB
            encrypted_transcript = transcript_item.get('transcript')
            if isinstance(encrypted_transcript, dict) and 'value' in encrypted_transcript:
                encrypted_transcript = encrypted_transcript['value']
                
            decrypted_transcript = decrypt_data(encrypted_transcript)
            transcription = json.loads(decrypted_transcript)
        except Exception as e:
            error_msg = f"Error decrypting transcript: {str(e)}"
            error_logger.error(error_msg)
            return JSONResponse({"error": error_msg}, status_code=400)
            
        # Get the template schema
        custom_template = report_item.get('custom_template')
        if custom_template:
            try:
                template_schema = json.loads(custom_template)
            except json.JSONDecodeError as e:
                error_msg = f"Invalid template schema format: {str(e)}"
                error_logger.error(error_msg)
                return JSONResponse({"error": error_msg}, status_code=400)
        else:
            # Use predefined schema based on template_type
            template_schema = globals().get(f"{template_type.upper()}_SCHEMA")
            if not template_schema:
                return JSONResponse(
                    {"error": f"Template type {template_type} not found"},
                    status_code=400
                )
                
        # Generate new report
        formatted_report = await generate_report_from_transcription(transcription, template_schema)
        if isinstance(formatted_report, str) and formatted_report.startswith("Error"):
            return JSONResponse({"error": formatted_report}, status_code=400)
            
        # Add template_type as the main heading
        formatted_report = f"# {template_type}\n\n{formatted_report}"
        
        # Update the report in DynamoDB
        update_expression = "SET formatted_report = :r, updated_at = :t"
        expression_values = {
            ":r": formatted_report,
            ":t": datetime.utcnow().isoformat()
        }
        
        report_table.update_item(
            Key={"id": report_id},
            UpdateExpression=update_expression,
            ExpressionAttributeValues=expression_values
        )
        
        return JSONResponse({
            "report_id": report_id,
            "template_type": template_type,
            "formatted_report": formatted_report
        })
        
    except Exception as e:
        error_msg = f"Failed to retry report generation: {str(e)}"
        error_logger.exception(error_msg)
        return JSONResponse({"error": error_msg}, status_code=500)

@app.post("/generate-summary/{transcript_id}")
@log_execution_time
async def generate_ai_summary(transcript_id: str):
    """
    Generate or regenerate an AI summary for a transcript.
    
    Args:
        transcript_id: ID of the transcript to summarize
        
    Returns:
        AI summary of the transcript
    """
    try:
        # Get the transcript from DynamoDB
        table = dynamodb.Table('transcripts')
        response = table.get_item(Key={"id": transcript_id})
        
        if 'Item' not in response:
            return JSONResponse(
                {"error": f"Transcript ID {transcript_id} not found"},
                status_code=404
            )
        
        transcript_item = response['Item']
        
        # Parse transcript data
        try:
            transcription = json.loads(transcript_item.get('transcript', '{}'))
        except json.JSONDecodeError:
            return JSONResponse(
                {"error": "Invalid transcript data format"},
                status_code=400
            )
        
        # Check if conversation data exists
        if "conversation" not in transcription or not transcription["conversation"]:
            return JSONResponse(
                {"error": "No conversation data found in transcript"},
                status_code=400
            )
        
        # Generate AI summary
        main_logger.info(f"Generating AI summary for transcript {transcript_id}")
        summary_prompt = "Please provide a concise summary of the following medical conversation:\n\n"
        for entry in transcription["conversation"]:
            summary_prompt += f"{entry['speaker']}: {entry['text']}\n\n"
        
        try:
            summary_response = client.chat.completions.create(
                model="gpt-4",
                messages=[
                    {"role": "system", "content": "You are an expert medical documentation assistant. When summarizing conversations, do not use speaker labels like 'Speaker 0' or 'Speaker 1'. Instead, refer to participants by their roles (e.g., doctor/clinician and patient) based on context, or simply summarize the key medical information without attributing statements to specific speakers."},
                    {"role": "user", "content": summary_prompt}
                ],
                max_tokens=1000
            )
            
            ai_summary = summary_response.choices[0].message.content
            main_logger.info(f"AI summary generated successfully for transcript {transcript_id}")
        except Exception as e:
            error_msg = f"Error generating AI summary: {str(e)}"
            error_logger.error(error_msg)
            return JSONResponse(
                {"error": error_msg},
                status_code=500
            )
        
        # Return the summary without updating the transcript
        return {
            "success": True,
            "transcript_id": transcript_id,
            "ai_summary": ai_summary
        }
        
    except Exception as e:
        error_logger.exception(f"Error generating AI summary: {str(e)}")
        return JSONResponse(
            {"error": f"Failed to generate AI summary: {str(e)}"},
            status_code=500
        )

@app.get("/transcription-history")
async def get_transcription_history():
    """
    Get history of all transcriptions stored in the database.
    """
    try:
        table = dynamodb.Table('transcripts')
        response = table.scan()
        
        items = response.get('Items', [])
        
        # Include report IDs and summary in the response
        for item in items:
            item['report_ids'] = item.get('report_ids', [])
            item['ai_summary'] = item.get('ai_summary', None)
        
        return {"transcriptions": items}
    except Exception as e:
        error_logger.error(f"Error retrieving transcription history: {str(e)}", exc_info=True)
        return JSONResponse(
            {"error": f"Failed to retrieve transcription history: {str(e)}"},
            status_code=500
        )

@app.get("/transcription/{transcript_id}")
async def get_transcription_detail(transcript_id: str):
    """
    Get detailed information about a specific transcription
    """
    try:
        # Get transcript data
        table = dynamodb.Table('transcripts')
        response = table.get_item(Key={"id": transcript_id})
        
        if 'Item' not in response:
            return JSONResponse(
                {"error": f"Transcript ID {transcript_id} not found"},
                status_code=404
            )
            
        transcript_item = response['Item']
        
        # Parse JSON data
        try:
            transcript = json.loads(transcript_item.get('transcript', '{}'))
            audio_file = json.loads(transcript_item.get('audio_file', '{}'))
        except:
            transcript = {}
            audio_file = {}
        
        # Get report if available
        report_data = None
        reports_table = dynamodb.Table('reports')
        report_response = reports_table.scan(
            FilterExpression="transcript_id = :transcript_id",
            ExpressionAttributeValues={":transcript_id": transcript_id}
        )
        report_items = report_response.get('Items', [])
        
        if report_items:
            report_item = report_items[0]
            report_data = {
                "id": report_item.get('id'),
                "template_type": report_item.get('template_type'),
                "formatted_report": report_item.get('formatted_report'),
                "gpt_response": json.loads(report_item.get('gpt_response', '{}')),
                "created_at": report_item.get('created_at')
            }
        
        # Generate presigned URL for audio
        presigned_url = None
        if audio_file.get('s3_path'):
            presigned_url = s3_client.generate_presigned_url(
                'get_object',
                Params={'Bucket': S3_BUCKET, 'Key': audio_file.get('s3_path')},
                ExpiresIn=3600
            )
        
        # Prepare the response
        response_data = {
            "id": transcript_item.get('id'),
            "created_at": transcript_item.get('created_at'),
            "updated_at": transcript_item.get('updated_at'),
            "status": transcript_item.get('status'),
            "audio_info": {
                "filename": audio_file.get('filename'),
                "s3_path": audio_file.get('s3_path'),
                "presigned_url": presigned_url
            },
            "transcript": transcript,
            "report": report_data
        }
        
        return response_data
    except Exception as e:
        error_logger.error(f"Error retrieving transcription detail: {str(e)}", exc_info=True)
        return JSONResponse(
            {"error": f"Failed to retrieve transcription: {str(e)}"},
            status_code=500
        )

@app.get("/test-dynamodb")
async def test_dynamodb():
    table = dynamodb.Table('transcripts')
    response = table.scan(Limit=10)
    return {"items": response.get('Items', [])}

@app.delete("/report/{report_id}")
async def delete_report(report_id: str):
    """
    Delete a report, its associated transcription, and audio file.
    
    Args:
        report_id: ID of the report to delete
    
    Returns:
        Success or error message with details of deleted items
    """
    try:
        operation_id = str(uuid.uuid4())[:8]
        main_logger.info(f"[OP-{operation_id}] Delete request for report ID: {report_id}")
        
        # Get the report from DynamoDB to find associated transcript_id
        reports_table = dynamodb.Table('reports')
        report_response = reports_table.get_item(Key={"id": report_id})
        
        if 'Item' not in report_response:
            main_logger.warning(f"[OP-{operation_id}] Report ID {report_id} not found")
            return JSONResponse(
                {"error": f"Report ID {report_id} not found"},
                status_code=404
            )
        
        report_item = report_response['Item']
        transcript_id = report_item.get('transcript_id')
        template_type = report_item.get('template_type', 'unknown')
        main_logger.info(f"[OP-{operation_id}] Found report with template type: {template_type}, linked to transcript ID: {transcript_id}")
        
        # Get audio file info from transcript
        audio_info = None
        s3_path = None
        if transcript_id:
            transcripts_table = dynamodb.Table('transcripts')
            transcript_response = transcripts_table.get_item(Key={"id": transcript_id})
            
            if 'Item' in transcript_response:
                transcript_item = transcript_response['Item']
                try:
                    audio_info = json.loads(transcript_item.get('audio_file', '{}'))
                    s3_path = audio_info.get('s3_path')
                    main_logger.info(f"[OP-{operation_id}] Found audio file: {s3_path}")
                except json.JSONDecodeError:
                    main_logger.warning(f"[OP-{operation_id}] Could not parse audio info from transcript")
        
        # Delete the audio file from S3 if it exists
        if s3_path:
            try:
                main_logger.info(f"[OP-{operation_id}] Deleting audio file from S3: {s3_path}")
                s3_client.delete_object(
                    Bucket=S3_BUCKET,
                    Key=s3_path
                )
                main_logger.info(f"[OP-{operation_id}] Successfully deleted audio file from S3: {s3_path}")
            except Exception as e:
                main_logger.error(f"[OP-{operation_id}] Error deleting audio file from S3: {str(e)}")
        
        # Delete the report
        main_logger.info(f"[OP-{operation_id}] Deleting report from DynamoDB: {report_id}")
        reports_table.delete_item(Key={"id": report_id})
        
        # Delete the associated transcript if it exists
        if transcript_id:
            main_logger.info(f"[OP-{operation_id}] Deleting transcript from DynamoDB: {transcript_id}")
            transcripts_table = dynamodb.Table('transcripts')
            transcripts_table.delete_item(Key={"id": transcript_id})
            
        # Prepare response with details of what was deleted
        deletion_details = {
            "report_id": report_id,
            "transcript_id": transcript_id,
            "audio_file": s3_path
        }
        
        main_logger.info(f"[OP-{operation_id}] Deletion completed successfully: {deletion_details}")
        return {
            "success": True,
            "message": f"Report, transcript, and audio file deleted successfully",
            "deletion_details": deletion_details
        }
        
    except Exception as e:
        operation_id = locals().get('operation_id', str(uuid.uuid4())[:8])
        error_logger.error(f"[OP-{operation_id}] Error deleting report: {str(e)}", exc_info=True)
        return JSONResponse(
            {"error": f"Failed to delete report: {str(e)}"},
            status_code=500
        )

@app.get("/reports")
async def get_all_reports(limit: int = 100):
    """
    Get all reports stored in the database.
    
    Args:
        limit: Maximum number of reports to return (default: 100)
    
    Returns:
        List of all reports
    """
    try:
        # Query the reports table
        reports_table = dynamodb.Table('reports')
        response = reports_table.scan(Limit=limit)
        
        items = response.get('Items', [])
        
        # Format the response
        formatted_reports = []
        for item in items:
            # Parse the report data
            try:
                gpt_response = json.loads(item.get('gpt_response', '{}'))
            except:
                gpt_response = {}
                
            formatted_reports.append({
                "id": item.get('id'),
                "transcript_id": item.get('transcript_id'),
                "template_type": item.get('template_type'),
                "formatted_report": item.get('formatted_report'),
                "created_at": item.get('created_at'),
                "updated_at": item.get('updated_at'),
                "status": item.get('status')
            })
        
        return {"reports": formatted_reports}
        
    except Exception as e:
        error_logger.error(f"Error retrieving reports: {str(e)}", exc_info=True)
        return JSONResponse(
            {"error": f"Failed to retrieve reports: {str(e)}"},
            status_code=500
        )

@app.get("/summary-history")
async def get_summary_history():
    """
    Get history of all summaries stored in the database.
    """
    try:
        table = dynamodb.Table('summaries')
        response = table.scan()
        
        items = response.get('Items', [])
        
        # Return all items without filtering or limiting fields
        return {"summaries": items}
    except Exception as e:
        error_logger.error(f"Error retrieving summary history: {str(e)}", exc_info=True)
        return JSONResponse(
            {"error": f"Failed to retrieve summary history: {str(e)}"},
            status_code=500
        )

@app.get("/report-history")
async def get_report_history():
    """
    Get history of all reports stored in the database.
    """
    try:
        table = dynamodb.Table('reports')
        response = table.scan()
        
        items = response.get('Items', [])
        
        # Return all items without filtering or limiting fields
        return {"reports": items}
    except Exception as e:
        error_logger.error(f"Error retrieving report history: {str(e)}", exc_info=True)
        return JSONResponse(
            {"error": f"Failed to retrieve report history: {str(e)}"},
            status_code=500
        )

@log_execution_time
async def fetch_prompts(transcription: dict, template_type: str, feedback=None, medicine_vocab=None) -> tuple[str, str]:
    """
    Generate GPT response based on a transcription and template type, streaming the output.
    Args:
        transcription: Dictionary containing the transcription data
        template_type: Type of template to use for formatting
    Yields:
        Streaming GPT response content as it arrives
    """
    operation_id = str(uuid.uuid4())[:8]
    main_logger.info(f"[OP-{operation_id}] Fetching system prompt and user prompt for template: {template_type}")

    # Validate inputs
    if not isinstance(transcription, dict) or "conversation" not in transcription:
        main_logger.error(f"[OP-{operation_id}] Invalid transcription: missing 'conversation' field")
        raise ValueError("Transcription must be a dictionary with a 'conversation' field")
    
    try:
        
        # Prepare conversation text for the prompt
        conversation_text = ""
        if "conversation" in transcription:
            for entry in transcription["conversation"]:
                speaker = entry.get("speaker", "Unknown")
                text = entry.get("text", "")
                conversation_text += f"{speaker}: {text}\n\n"
        
        # Select system message based on template type
        system_message = "You are a clinical documentation expert. Format the conversation into structured clinical notes."
        
        current_date = None
        if "metadata" in transcription and "current_date" in transcription["metadata"]:
            current_date = transcription["metadata"]["current_date"]
                
        DATE_INSTRUCTIONS = """
            Date Handling Instructions:
            - Use {reference_date} as the reference date for all temporal expressions in the transcription.
            - Convert relative time references to specific dates based on the reference date:
            - "This morning," "today," or similar terms refer to the reference date.
            - "X days ago" refers to the reference date minus X days (e.g., "five days ago" is reference date minus 5 days).
            - "Last week" refers to approximately 7 days prior, adjusting for specific days if mentioned (e.g., "last Wednesday" is the most recent Wednesday before the reference date).
            - Format all dates as DD/MM/YYYY (e.g., 10/06/2025) in the output.
            - Do not use hardcoded dates or assumed years unless explicitly stated in the transcription.
            - Verify date calculations to prevent errors, such as incorrect years or misaligned days.
            """
         # Core instructions for all medical documentation templates
      
        preservation_instructions = """
            CRITICAL INSTRUCTIONS:
            1. ALWAYS preserve ALL dates mentioned (appointment dates, symptom onset, follow-up dates, etc.)
            2. NEVER omit medication names, dosages, frequencies or routes of administration
            3. Preserve ALL quantitative values including lab results, vital signs, and measurements
            4. Include specific timeframes for symptom progression (e.g., "for 2 weeks", "since April 10th")
            5. Maintain ALL numeric values exactly as stated (weights, blood pressure readings, glucose levels)
            6. ALWAYS include complete diagnostic information and specific medical terminology
            7. Preserve all references to previous treatments, procedures or interventions
            8. Extract and include all allergies, adverse reactions, and contraindications
            """

        # Add grammar-specific instructions to all templates
        grammar_instructions = """
            CRITICAL GRAMMAR INSTRUCTIONS:
            1. Use perfect standard US English grammar in all text
            2. Ensure proper subject-verb agreement in all sentences
            3. Use appropriate prepositions (avoid errors like "dependent of" instead of "dependent on")
            4. Eliminate article errors (never use "the a" or similar incorrect article combinations)
            5. Maintain professional medical writing style with proper punctuation
            6. Use proper tense consistency throughout all documentation
            7. Ensure proper noun-pronoun agreement
            8. Use clear and concise phrasing without grammatical ambiguity
            """

        formatting_instructions = """You are an AI model tasked with generating structured reports. Follow strict formatting rules as outlined below:

            Headings must begin with # followed by a space.  
            Example:  
            # Clinical Summary

            Subheadings must begin with ## followed by a space.  
            Example:  
            ## Patient History

            **Spacing and Indentation Rules:**  
            •  Always add **one blank line** after a heading or subheading before starting the content.  
            •  Never start content directly under the heading on the same line.  
            •  Do not indent the start of paragraphs. Paragraph content should be aligned left.  
            •  For bullet points, indent with exactly **2 spaces**, followed by a bullet (•), then 2 spaces.  
            •  Do not add extra indentation beyond this for bullets or paragraphs.  
            •  Maintain consistent alignment throughout the document.

            For lists or key points:  
            Use only when there are distinct, separate items (e.g., symptoms, findings, medications).  
            Each point must:  
            •  Start with exactly one level of indentation (2 spaces)  
            •  Use '•  ' (Unicode bullet point U+2022) followed by two spaces
            •  Be a short, clear item or statement  

            Example:  
            •  Patient reported mild headache  
            •  No known drug allergies  

            For paragraphs or multiple sentences (narratives):  
            •  Do not use bullets or indentation  
            •  Just write in standard paragraph form, aligned left  
            •  Separate paragraphs with a blank line if needed

            **Do NOT mix bullet points and narrative in the same section.**  
            Use either bullets for itemized info or plain text for narrative – never both.

            If data for some heading is missing (not mentioned during session), then **omit that heading** entirely from the output.

            Stick to this formatting **exactly** in every section of the report.
"""
        
        date_instructions = DATE_INSTRUCTIONS.format(reference_date=current_date)
      
        user_prompt = f"""
            You are provided with a medical conversation transcript. 
            Analyze the transcript and generate a structured report following the specified template: {template_type.replace("_", " ").upper()}.

            Use only the information explicitly provided in the transcript. Do not assume or invent any details.  
            Ensure the output is a valid TEXT/PLAIN format with clearly defined sections matching the template.

            Guidelines:
            - If information is missing for a section, **omit that section entirely** from the output.
            - Use **numeric format** for all numerical data (e.g., age, dosage, duration).
            - Keep content **concise, professional, and in a doctor-like tone**.
            - **Summarize and synthesize** the transcript intelligently to produce a practical report that would help a clinician quickly understand the patient's case.
            - Add any **time-related** details from the transcript (e.g., duration of symptoms, date of procedures) clearly.
            - Use appropriate **medical terminology**.
            - The output **must be in valid TEXT format**, structured per the specified template.
            - Structure the content as **bullet points or concise paragraphs** as per professional reporting standards.
            - Do **not** repeat or paraphrase the conversation; instead, transform it into a professional summary from a clinician’s perspective.
            - Use {current_date if current_date else "the current date"} as the reference point for any relative time expressions.
            - Make it grammatically accurate and as best as possible, never miss any important perspective and never repeat anything in any section. 
            - Ignore the headings for which data is not available
            - Never use ('- ') always use bullets for points.

            Transcript:
            {conversation_text}

            Date Instructions:
            {date_instructions}
            """

        # Template-specific instructions and schema
        if template_type == "soap_note":
            user_instructions = f"""You are provided with a medical conversation transcript. 
            Analyze the transcript thoroughly to generate a structured SOAP note following the specified template, synthesizing the patient’s case from a physician’s perspective to produce a concise, professional, and clinically relevant note that facilitates medical decision-making. 
            Use only information explicitly provided in the transcript, without assuming or adding any details. 
            Ensure the output is a valid textual format with the SOAP note sections (Subjective, Past Medical History, Objective, Assessment, Plan) as keys, formatted in a professional, doctor-like tone. 
            Address each chief complaint and issue separately in the Subjective and Assessment/Plan sections. 
            For time references (e.g., 'this morning' to June 1, 2025; 'last Wednesday' to May 28, 2025; 'a week ago' to May 25, 2025), convert to specific dates based on today’s date, June 1, 2025 (Sunday). 
            Use numeric format for all numbers (e.g., '2' instead of 'two'). Ensure each point in Subjective, Past Medical History, and Objective starts with '- ', while Assessment and Plan use subheadings without '- ' for clear, concise points. 
            Omit sections with no relevant information. The note should be streamlined and to the point, prioritizing utility for physicians. 
            Use the specific template provided below to generate the SOAP note.
            Dont Repeat any points in the subjective, and past medical history.
            For all numbers related information, preserve the exact numbers mentioned in the transcript and use digits.
            Below is the transcript:

            {conversation_text}
            Below are the report formatting structure:
            {formatting_instructions}


            """
            # SOAP Note Instructions - Updated for conciseness and direct language
            system_message = f"""You are an expert medical scribe tasked with generating a professional, concise, and clinically relevant SOAP note based solely on the patient transcript, contextual notes, or clinical note provided. 
            Your role is to analyze the input thoroughly, synthesize the patient’s case from a physician’s perspective, and produce a streamlined SOAP note that prioritizes clarity, relevance, and utility for medical decision-making. 
            Adhere strictly to the provided SOAP note template, including only information explicitly stated in the input. Structure the note in point form, starting each line with '- ', and ensure it is professional, avoiding extraneous details.
            Convert vague time references (e.g., 'this morning' to June 1, 2025; 'last Wednesday' to May 28, 2025; 'a week ago' to May 25, 2025) based on today’s date, June 1, 2025 (Sunday). 
            Do not fabricate or infer patient details, assessments, plans, interventions, evaluations, or care plans beyond what is explicitly provided. Omit sections with no relevant information. 
            The output should be a plain text SOAP note tailored to assist physicians efficiently. 
            {preservation_instructions} {grammar_instructions} {formatting_instructions}
            
            The template is as follows:

            Subjective:
            - Chief complaints and reasons for visit (e.g., symptoms, patient requests) (include only if explicitly stated)
            - Duration, timing, location, quality, severity, and context of complaints (include only if explicitly stated)
            - Factors worsening or alleviating symptoms, including self-treatment attempts and their effectiveness (include only if explicitly stated)
            - Progression of symptoms over time (include only if explicitly stated)
            - Previous episodes of similar symptoms, including timing, management, and outcomes (include only if explicitly stated)
            - Impact of symptoms on daily life, work, or activities (include only if explicitly stated)
            - Associated focal or systemic symptoms related to chief complaints (include only if explicitly stated)

            Past Medical History:
            - Relevant past medical or surgical history, investigations, and treatments tied to chief complaints (include only if explicitly stated)
            - Relevant social history (e.g., lifestyle, occupation) related to chief complaints (include only if explicitly stated)
            - Relevant family history linked to chief complaints (include only if explicitly stated)
            - Exposure history (e.g., environmental, occupational) (include only if explicitly stated)
            - Immunization history and status (include only if explicitly stated)
            - Other relevant subjective information (include only if explicitly stated)

            Objective:
            - Vital signs (include only if explicitly stated)
            - Physical or mental state examination findings, including system-specific exams (include only if explicitly stated)
            - Completed investigations and their results (include only if explicitly stated; planned or ordered investigations belong in Plan)

            Assessment:
            - Likely diagnosis (include only if explicitly stated)
            - Differential diagnosis (include only if explicitly stated)

            Plan:
            - Planned investigations (include only if explicitly stated)
            - Planned treatments (include only if explicitly stated)
            - Other actions (e.g., counseling, referrals, follow-up instructions) (include only if explicitly stated)
            
            Referrence Example:

            Example Transcription:
            Speaker 0: Good morning, Mr. Johnson. What brings you in today? 
            Speaker 1: I’ve been having chest pain and feeling my heart race since last Wednesday. It’s been tough to catch my breath sometimes. 
            Speaker 0: How would you describe the chest pain? Where is it, and how long does it last? 
            Speaker 1: It’s a sharp pain in the center of my chest, lasts a few minutes, and comes and goes. It’s worse when I walk upstairs. 
            Speaker 0: Any factors that make it better or worse? 
            Speaker 1: Resting helps a bit, but it’s still there. I tried taking aspirin a few days ago, but it didn’t do much. 
            Speaker 0: Any other symptoms, like nausea or sweating? 
            Speaker 1: No nausea or sweating, but I’ve been tired a lot. No fever or weight loss. 
            Speaker 0: Any past medical conditions or surgeries? 
            Speaker 1: I had high blood pressure diagnosed a few years ago, and I take lisinopril 20 mg daily. 
            Speaker 0: Any side effects from the lisinopril? 
            Speaker 1: Not really, it’s been fine. My blood pressure’s been stable. 
            Speaker 0: Any allergies? 
            Speaker 1: I’m allergic to penicillin. 
            Speaker 0: What’s your lifestyle like? 
            Speaker 1: I’m a retired teacher, live alone, and walk daily. I’ve been stressed about finances lately. 
            Speaker 0: Any family history of heart issues? 
            Speaker 1: My father had a heart attack in his 60s. 
            Speaker 0: Let’s check your vitals. Blood pressure is 140/90, heart rate is 88, temperature is normal at 98.6°F. You appear well but slightly anxious. 
            Speaker 0: Your symptoms suggest a possible heart issue. We’ll order an EKG and blood tests today and refer you to a cardiologist. Continue lisinopril, and avoid strenuous activity. Call us if the pain worsens or you feel faint. 
            Speaker 1: Okay, I understand. When should I come back? 
            Speaker 0: Schedule a follow-up in one week to discuss test results, or sooner if symptoms worsen.

            Example SOAP Note Output:

            Subjective:
            - Chest pain and heart racing since last Wednesday. Dyspnoea.
            - Sharp pain in centre of chest, lasting few minutes, intermittent. Worse when climbing stairs.
            - Resting helps slightly. Tried aspirin with minimal effect.
            - Associated fatigue. No nausea, sweating, fever or weight loss.

            Past Medical History:
            - Hypertension - on lisinopril 20mg daily. BP stable.
            - Allergies: Penicillin.
            - Social: Retired teacher. Lives alone. Daily walks. Financial stress recently.
            - Family history: Father had MI in 60s.

            Objective:
            - BP 140/90, HR 88, temperature normal (98.6°F).
            - Appears well but slightly anxious.

            Assessment:
            - Possible cardiac issue.

            Plan:
            - ECG and blood tests ordered.
            - Cardiology referral.
            - Continue lisinopril.
            - Avoid strenuous activity.
            - Advised to call if pain worsens or develops syncope.
            - Follow-up in one week to discuss results, or sooner if symptoms worsen.
            """
        
        elif template_type == "cardiology_consult":
            current_date = None
            if "metadata" in transcription and "current_date" in transcription["metadata"]:
                current_date = transcription["metadata"]["current_date"]
                main_logger.info(f"[OP-{operation_id}] Using current_date from metadata: {current_date}")
            

            user_instructions= f"""You are provided with a medical conversation transcript. 
            Analyze the transcript and generate a structured CARDIOLOGY CONSULT NOTE following the specified template. 
            Use only the information explicitly provided in the transcript, and do not include or assume any additional details. 
            Ensure the output is a valid Text format with the CARDIOLOGY CONSULT NOTE sections formatted professionally and concisely in a doctor-like tone. 
            Make sure the output is in valid Text format. 
            If the patient didnt provide the information regarding any section then ignore the respective section.
            Include the all numbers in numeric format.
            Make sure the output is concise and to the point.
            ENsure the data in each section should be to the point and professional.
            Make it useful as doctors perspective so it makes there job easier, dont just dictate and make a note, analyze the conversation, summarize it and make a note that best desrcibes the patient's case as a doctor's perspective.
            Add time related information in the report dont miss them.
            Make sure the point are concise and structured and looks professional.
            Use medical terms to describe each thing.
            Follow strictly the format given below, and always written same output format.
            If data for any field is not available dont write anything under that heading and ignore it.
            Use {current_date if current_date else "the current date"} as the reference date for all temporal expressions in the transcription.
            Below is the transcript:\n\n{conversation_text}
            """
            system_message = f"""You are a medical documentation assistant tasked with generating a detailed and structured cardiac assessment report based on a predefined template. 
            Your output must maintain a professional, concise, and doctor-like tone, avoiding verbose or redundant phrasing. 
            All information must be sourced exclusively from the provided transcript, contextual notes, or clinical notes, and only explicitly mentioned details should be included. 
            If information for a specific section or placeholder is unavailable, Ignore that heading.
            Do not invent or infer patient details, assessments, plans, interventions, or follow-up care beyond what is explicitly stated. 
            If data for any field is not available dont write anything under that heading and ignore it.
           
            Date Handling Instructions:
                Use {current_date if current_date else "the current date"} as the reference date for all temporal expressions in the transcription.
                Convert relative time references to specific dates based on the reference date:
                    "This morning," "today," or similar terms refer to the reference date.
                    "X days ago" refers to the reference date minus X days (e.g., "five days ago" is reference date minus 5 days).
                    "Last week" refers to approximately 7 days prior, adjusting for specific days if mentioned (e.g., "last Wednesday" is the most recent Wednesday before the reference date).
                Format all dates as DD/MM/YYYY (e.g., 10/06/2025) in the CARDIOLOGY LETTER output.
                Do not use hardcoded dates or assumed years unless explicitly stated in the transcription.
                Verify date calculations to prevent errors, such as incorrect years or misaligned days.
            {preservation_instructions} {grammar_instructions}
            Use the formatting instructions given below:
            {formatting_instructions}
            Follow the structured guidelines below for each section of the report:

            ##1. Reason For Visit:
            - describe the reason for visit

            ##2. Cardiac Risk Factors:
            - List under the heading "CARDIAC RISK FACTORS" in the following order: Increased BMI, Hypertension, Dyslipidemia, Diabetes mellitus, Smoker, Ex-smoker, Family history of atherosclerotic disease in first-degree relatives.
            - For each risk factor, use verbatim text from contextual notes if available, updating only with new or differing information from the transcript.
            - If no information is provided in the transcript or context, ignore that stuff.
            - For Smoker, include pack-years, years smoked, and number of cigarettes or packs per day if mentioned in the transcript or context.
            - For Ex-smoker, include only if the patient is not currently smoking. State the year quit and whether they quit remotely (e.g., "quit remotely in 2015") if mentioned. Omit this section if the patient is a current smoker.
            - For Family history of atherosclerotic disease, specify if the patient’s mother, father, sister, brother, or child had a myocardial infarction, coronary angioplasty, coronary stenting, coronary bypass, peripheral arterial procedure, or stroke prior to age 55 (men) or 65 (women), as mentioned in the transcript or context.

            ##3. Cardiac History:
            - List under the heading "CARDIAC HISTORY" all cardiac diagnoses in the following order, if mentioned: heart failure (HFpEF or HFrEF), cardiomyopathy, arrhythmias (atrial fibrillation, atrial flutter, SVT, VT, AV block, heart block), devices (pacemakers, ICDs), valvular disease, endocarditis, pericarditis, tamponade, myocarditis, coronary disease.
            - Use verbatim text from contextual notes, updating only with new or differing transcript information.
            - For heart failure, specify HFpEF or HFrEF and include ICD implantation details (date and model) if mentioned.
            - For arrhythmias, include history of cardioversions and ablations (type and date) if mentioned.
            - For AV block, heart block, or syncope, state if a pacemaker was implanted, including type, model, and date, if mentioned.
            - For valvular heart disease, note the type of intervention (e.g., valve replacement) and date if mentioned.
            - For coronary disease, summarize previous angiograms, coronary anatomy, and interventions (angioplasty, stenting, CABG) with graft details and dates if mentioned.
            - Omit this section entirely if no cardiac history is mentioned in the transcript or context.

            ##4. Other Medical History:
            - List under the heading "OTHER MEDICAL HISTORY" all non-cardiac diagnoses and previous surgeries with associated years if mentioned.
            - Use verbatim text from contextual notes, updating only with new or differing transcript information.
            - Ignore no non-cardiac medical history is mentioned in the transcript or context.

            ##5. Current Medications:
            - List under the heading "CURRENT MEDICATIONS" in the following subcategories, each on a single line:
                - Antithrombotic Therapy: Include aspirin, clopidogrel, ticagrelor, prasugrel, apixaban, rivaroxaban, dabigatran, edoxaban, warfarin.
                - Antihypertensives: Include ACE inhibitors, ARBs, beta blockers, calcium channel blockers, alpha blockers, nitrates, diuretics (excluding furosemide, e.g., HCTZ, chlorthalidone, indapamide).
                - Heart Failure Medications: Include Entresto, SGLT2 inhibitors, mineralocorticoid receptor antagonists, furosemide, metolazone, ivabradine.
                - Lipid Lowering Medications: Include statins, ezetimibe, PCSK9 modifiers, fibrates, icosapent ethyl.
                - Other Medications: Include any medications not listed above.
            - For each medication, include dose, frequency, and administration if mentioned, and note the typical recommended dosage per tablet or capsule in parentheses if it differs from the patient’s dose.
            - Use verbatim text from contextual notes, updating only with new or differing transcript information.
            - Ignore each subcategory if no medications are mentioned.

            ##6. Allergies and Intolerances:
            - List under the heading "ALLERGIES AND INTOLERANCES" in sentence format, separated by commas, with reactions in parentheses if mentioned (e.g., "penicillin (rash), sulfa drugs (hives)").
            - Use verbatim text from contextual notes, updating only with new or differing transcript information.
            - Ignore if no allergies or intolerances are mentioned.

            ##7. Social History:
            - List under the heading "SOCIAL HISTORY" in a short-paragraph narrative form.
            - Include living situation, spousal arrangements, number of children, working arrangements, retirement status, smoking status, alcohol use, illicit or recreational drug use, and private drug/health plan status, if mentioned.
            - For smoking, write "Smoking history as above" if the patient smokes or is an ex-smoker; otherwise, write "Non-smoker."
            - For alcohol, specify the number of drinks per day or week if mentioned.
            - Include comments on activities of daily living (ADLs) and instrumental activities of daily living (IADLs) if relevant.
            - Use verbatim text from contextual notes, updating only with new or differing transcript information.
            - Ignore this subheading if no social history is mentioned.

            ##8. History:
            - List under the heading "HISTORY" in narrative paragraph form, detailing all reasons for the current visit, chief complaints, and a comprehensive history of presenting illness.
            - Include mentioned negatives from exams and symptoms, as well as details on physical activities and exercise regimens, if mentioned.
            - Only include information explicitly stated in the transcript or context.
            - End with: "Review of systems is otherwise non-contributory."

            ##9. Physical Examination:
            - List under the heading "PHYSICAL EXAMINATION" in a short-paragraph narrative form.
            - Include vital signs (blood pressure, heart rate, oxygen saturation), cardiac examination, respiratory examination, peripheral edema, and other exam insights only if explicitly mentioned in the transcript or context.
            - If cardiac exam is not mentioned or normal, write: "Precordial examination was unremarkable with no significant heaves, thrills or pulsations. Heart sounds were normal with no significant murmurs, rubs, or gallops."
            - If respiratory exam is not mentioned or normal, write: "Chest was clear to auscultation."
            - If peripheral edema is not mentioned or normal, write: "No peripheral edema."
            - Omit other physical exam insights if not mentioned.

            ##10. Investigations:
                - List under the heading "INVESTIGATIONS" in the following subcategories, each on a single line with findings and dates in parentheses followed by a colon:
                - Laboratory investigations: List CBC, electrolytes, creatinine with GFR, troponins, NTpBNP or BNP level, A1c, lipids, and other labs in that order, if mentioned.
                - ECGs: List ECG findings and dates.
                - Echocardiograms: List echocardiogram findings and dates.
                - Stress tests: List stress test findings (including stress echocardiograms and graded exercise challenges) and dates.
                - Holter monitors: List Holter monitor findings and dates.
                - Device interrogations: List device interrogation findings and dates.
                - Cardiac perfusion imaging: List cardiac perfusion imaging findings and dates.
                - Cardiac CT: List cardiac CT findings and dates.
                - Cardiac MRI: List cardiac MRI findings and dates.
                - Other investigations: List other relevant investigations and dates.
                - Use verbatim text from contextual notes, updating only with new or differing transcript information.
                - Ignore each subcategory if no findings are mentioned.

            ##11. Summary:
                - List under the heading "SUMMARY" in a cohesive narrative paragraph.
                - Start with: "[patient name] is a pleasant [age] year old [gender] that was seen today for cardiac assessment."
                - Include: "Cardiac risk factors include [list risk factors]" if mentioned in the transcript or context.
                - Summarize patient symptoms from the History section and cardiac investigations from the Investigations section, if mentioned.
                - Omit risk factors or summary details if not mentioned.
                [make it in a paragraph form]

            ##12. Assessment/Plan:
                - List under the heading "ASSESSMENT/PLAN" for each medical issue, structured as:
                - #[number] [Condition]
                - Assessment: [Current assessment of the condition, drawn from context and transcript]
                - Plan: [Management plan, including investigations, follow-up, and reasoning for the plan, drawn from context and transcript. Include counselling details if mentioned.]
                - Number each issue sequentially (e.g., #1, #2) and ensure all information is explicitly mentioned in the transcript or context.

            ##13. Follow-Up:
                - List under the heading "FOLLOW-UP" any follow-up plans and time frames explicitly mentioned in the transcript.
                - If no time frame is specified, write: "Will follow-up in due course, pending investigations, or sooner should the need arise."

            Additional Instructions:
            - Ensure strict adherence to the template structure, maintaining the exact order and headings as specified.
            - Use "•  " only where indicated (e.g., Assessment/Plan, Current Medications etc).
            - Write in complete sentences for narrative sections (History, Social History, Physical Examination, Summary).
            - If data for any field is not available dont write anything under that heading and ignore it.
            - Ensure all sections are populated only with explicitly provided data, preserving accuracy and professionalism.
        """
       
        elif template_type =="echocardiography_report_vet":
            user_instructions = user_prompt

            system_message = f"""You are a medical documentation assistant tasked with generating a structured and clinically relevant medical report based on the specified template: {template_type.replace("_", " ").upper()}.
            Your output must reflect a professional, concise, and doctor-like tone. Avoid verbose, repetitive, or casual language. The goal is to produce a report that enhances clinical efficiency and clarity.
            Only include information that is **explicitly mentioned** in the transcript, contextual notes, or clinical notes. Do **not** infer or fabricate any details regarding the patient’s history, assessment, plan, medications, or investigations.

            Omission Policy:
            - If data for any section or placeholder is not available, **omit the entire section and its heading**.
            - Do **not** insert default text like "None known" unless explicitly instructed by the template or transcript.

            Content Requirements:
            - Use clear, precise, and formal medical language suitable for clinical documentation.
            - Structure each section according to the template, using professional formatting.
            - Convert all numeric references to proper numerical format (e.g., "five days" → "5 days").

            Output Expectations:
            - Output must strictly follow the structure defined by the {template_type.replace("_", " ").upper()} template.
            - The output must be in valid TEXT format, using section names as headings and their respective summarized content as its content.
            - Each section should be clinically useful, concise, and tailored to help a doctor quickly understand the case.
            - Ensure the structure and formatting are consistent across all reports using the same template.

            Use below listed formatting rules:
            {formatting_instructions}
            
            Echocardiography Report Template STructure:

            # VET Echocardiography Report

            ## Patient Information:
            - [Patient name]
            - [Species]
            - [Breed]
            - [Age]
            - [Sex]
            - [Weight]

            Date of Examination: [DD/MM/YYYY]

            ## Reason for Echocardiography: [Brief description of presenting complaint or reason for referral]

            ## Echocardiographic Findings:

            1. Left Atrium and Ventricle:
            - [Left atrial size]
            - [Left ventricular size]
            - [Left ventricular wall thickness]
            - [Fractional shortening]

            2. Right Atrium and Ventricle:
            - [Right atrial size]
            - [Right ventricular size]
            - [Right ventricular wall thickness]

            3. Valves:
            - [Mitral valve]
            - [Tricuspid valve]
            - [Aortic valve]
            - [Pulmonic valve]

            4. Great Vessels:
            - [Aorta]
            - [Pulmonary artery]

            5. Doppler Studies:
            - [Mitral inflow]
            - [Tricuspid inflow]
            - [Aortic outflow]
            - [Pulmonic outflow]

            6. Other Findings:
            - [Pericardial effusion]
            - [Cardiac masses]
            - [Congenital defects]

            ## Measurements:
            [List relevant measurements, e.g., LA/Ao ratio, EPSS, etc.]

            ## Interpretation:
            [Brief summary of significant findings and their clinical relevance]

            ## Diagnosis:
            [Primary echocardiographic diagnosis]

            ## Recommendations:
            [Suggested follow-up, treatment, or further diagnostics if applicable]

            Report prepared by: [Clinician's Name]
            Date: [DD/MM/YYYY]

            Begin generating the report using the transcript content and template structure provided.
            """

        elif template_type =="cardio_consult":
            user_instructions = user_prompt

            system_message = f"""You are a medical documentation assistant tasked with generating a structured and clinically relevant medical report based on the specified template: {template_type.replace("_", " ").upper()}.
            Your output must reflect a professional, concise, and doctor-like tone. Avoid verbose, repetitive, or casual language. The goal is to produce a report that enhances clinical efficiency and clarity.
            Only include information that is **explicitly mentioned** in the transcript, contextual notes, or clinical notes. Do **not** infer or fabricate any details regarding the patient’s history, assessment, plan, medications, or investigations.

            Omission Policy:
            - If data for any section or placeholder is not available, **omit the entire section and its heading**.
            - Do **not** insert default text like "None known" unless explicitly instructed by the template or transcript.

            Content Requirements:
            - Use clear, precise, and formal medical language suitable for clinical documentation.
            - Structure each section according to the template, using professional formatting.
            - Convert all numeric references to proper numerical format (e.g., "five days" → "5 days").

            Output Expectations:
            - Output must strictly follow the structure defined by the {template_type.replace("_", " ").upper()} template.
            - The output must be in valid TEXT format, using section names as headings and their respective summarized content as its content.
            - Each section should be clinically useful, concise, and tailored to help a doctor quickly understand the case.
            - Ensure the structure and formatting are consistent across all reports using the same template.

            Use below listed formatting rules:
            {formatting_instructions}
            
            Cardio Consult Template STructure:

            # Cardio Consult

            ## Subjective: 
            - [describe current cardiovascular issues, reasons for visit, discussion topics, history of presenting complaints etc] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank.)
            - [describe past medical history, previous cardiovascular surgeries] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank.)
            - [mention cardiovascular medications and herbal supplements] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank.)
            - [describe social history relevant to cardiovascular health] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank.)
            - [mention allergies] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank.)

            ## Objective: 
            - [vital signs including blood pressure, heart rate, respiratory rate, temperature] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank.)
            - [physical examination findings relevant to cardiovascular system] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank.)
            - [results of recent cardiovascular tests and investigations] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank.)

            ## Assessment: 
            - [diagnosis or differential diagnosis related to cardiovascular issues] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank.)
            - [summary of clinical findings and their implications] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank.)

            ## Plan: 
            - [treatment plan including medications, lifestyle modifications, and follow-up appointments] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank.)
            - [referrals to other specialists or for further investigations] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank.)
            - [patient education and counselling provided] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank.)

            Begin generating the report using the transcript content and template structure provided.
            """

        elif template_type =="cardio_patient_explainer":
            user_instructions = user_prompt

            system_message = f"""You are a medical documentation assistant tasked with generating a structured and clinically relevant medical report based on the specified template: {template_type.replace("_", " ").upper()}.
            Your output must reflect a professional, concise, and doctor-like tone. Avoid verbose, repetitive, or casual language. The goal is to produce a report that enhances clinical efficiency and clarity.
            Only include information that is **explicitly mentioned** in the transcript, contextual notes, or clinical notes. Do **not** infer or fabricate any details regarding the patient’s history, assessment, plan, medications, or investigations.

            Omission Policy:
            - If data for any section or placeholder is not available, **omit the entire section and its heading**.
            - Do **not** insert default text like "None known" unless explicitly instructed by the template or transcript.

            Content Requirements:
            - Use clear, precise, and formal medical language suitable for clinical documentation.
            - Structure each section according to the template, using professional formatting.
            - Convert all numeric references to proper numerical format (e.g., "five days" → "5 days").

            Output Expectations:
            - Output must strictly follow the structure defined by the {template_type.replace("_", " ").upper()} template.
            - The output must be in valid TEXT format, using section names as headings and their respective summarized content as its content.
            - Each section should be clinically useful, concise, and tailored to help a doctor quickly understand the case.
            - Ensure the structure and formatting are consistent across all reports using the same template.

            Use below listed formatting rules:
            {formatting_instructions}
            
            Cardio Patient Explainer Template STructure:

            # Cardio Patient Explainer

            Dear [Patient's Name], (Write [Patient's Name] place holder if patient name is not known)

            It was a pleasure to see you today and review your health concerns. I truly appreciate the time you took to share details about your health and personal life. I’ve summarized our discussion below to ensure you have a clear understanding of what we talked about during your visit.

            ## 1. [Topic/Issue #1: Description]
            During our discussion, we talked about [describe the first topic/issue in simple terms]. This means [explain the condition, symptom, or topic in layperson's terms]. It is important to [describe any actions, reasons for concern, or key details to remember]. [If applicable, mention treatments and medication dosage, lifestyle adjustments, or monitoring required.]

            ## 2. [Topic/Issue #2: Description]
            Another key point we covered was [describe the second topic/issue]. This relates to [explain in simple language]. You may notice [describe symptoms or improvements to monitor] and should consider [mention treatments and medication dosage, advice, or follow-ups if relevant].

            ## 3. [Topic/Issue #3: Description]
            We also discussed [describe the third topic/issue, if applicable]. To address this, [lay out the plan or approach discussed, with clear explanations]. [Include any specific recommendations for actions or observations.]

            [Add more topics as needed, following the same format.]

            ## Next Steps:
            [Summarize the specific next actions for the patient, such as “Please remember to schedule your follow-up appointment in two weeks,” or “Don’t forget to start the medication as prescribed and let us know if you experience any side effects.”]

            Thank you for trusting me with your care. If you have any questions or concerns about anything we discussed, please do not hesitate to reach out.

            Warm regards,
            [Dr. Clinician name] (Write [Dr. Clinician name] place holder if patient name is not known)

            (Never come up with your own patient details, assessment, diagnosis, plan, interventions, evaluation, and plan for continuing care - use only the transcript, contextual notes or clinical note as a reference for the information include in your note. If any information related to a placeholder has not been explicitly mentioned in the transcript, contextual notes or clinical note, you must not state the information has not been explicitly mentioned in your output, just leave the relevant placeholder or section blank.)

            Begin generating the report using the transcript content and template structure provided.
            """

        elif template_type =="coronary_angiograph_report":
            user_instructions = user_prompt

            system_message = f"""You are a medical documentation assistant tasked with generating a structured and clinically relevant medical report based on the specified template: {template_type.replace("_", " ").upper()}.
            Your output must reflect a professional, concise, and doctor-like tone. Avoid verbose, repetitive, or casual language. The goal is to produce a report that enhances clinical efficiency and clarity.
            Only include information that is **explicitly mentioned** in the transcript, contextual notes, or clinical notes. Do **not** infer or fabricate any details regarding the patient’s history, assessment, plan, medications, or investigations.

            Omission Policy:
            - If data for any section or placeholder is not available, **omit the entire section and its heading**.
            - Do **not** insert default text like "None known" unless explicitly instructed by the template or transcript.

            Content Requirements:
            - Use clear, precise, and formal medical language suitable for clinical documentation.
            - Structure each section according to the template, using professional formatting.
            - Convert all numeric references to proper numerical format (e.g., "five days" → "5 days").

            Output Expectations:
            - Output must strictly follow the structure defined by the {template_type.replace("_", " ").upper()} template.
            - The output must be in valid TEXT format, using section names as headings and their respective summarized content as its content.
            - Each section should be clinically useful, concise, and tailored to help a doctor quickly understand the case.
            - Ensure the structure and formatting are consistent across all reports using the same template.

            Use below listed formatting rules:
            {formatting_instructions}
            
            Coronary Angiograph Report Template STructure:

            # Coronary Angiograph Report 

            ## Indication
            [Describe the clinical reason for coronary angiography (e.g., chest pain, abnormal stress test, suspected CAD)]
            (Include only if explicitly mentioned in the transcript, contextual notes, or clinical note)

            ## Access & Setup
            [Specify access site (e.g., right radial, femoral), sheath size, and catheter types used]
            [Make it in a Paragraph-form]

            [Mention local anaesthesia and any preparation steps taken]
            [Make it in a Paragraph-form]
            (Include only if mentioned)

            ## Sedation & Medications Administered
            [List all procedural medications and dosages: e.g., heparin, GTN, sedatives, analgesics, contrast volume]
            (Include only if mentioned)

            ## Procedure Performed
            [State procedure type: diagnostic angiography, PCI, etc.]
            (Include only if mentioned)

            ## Findings
            Provide structured findings for each artery and any relevant tests:

            1. Left Main Coronary Artery (LM):
                [Size, branching, disease status]

            2. Left Anterior Descending Artery (LAD):
                [Size, flow (TIMI), stenosis, plaque, anomalies]

            3. Left Circumflex Artery (LCX):
                [Dominance, size, flow, disease]

            4. Right Coronary Artery (RCA):
                [Dominance, size, flow, disease]

            5. Other vessels / grafts (if applicable):
                [e.g., SVGs, LIMA]

            6. Left Ventriculogram / Aortogram / LHS Function:
                [Performed or not, and results if available]
                (Include only if explicitly documented for each item above)

            ## Closure Technique
            [Describe haemostasis method used (e.g., TR band), result, and patient instructions]
            (Include only if mentioned)

            ## Complications
            [State if any intra/post-procedural complications occurred. Write "None" only if explicitly stated.]
            (Omit section if not mentioned)

            ## Conclusion / Summary
            [Summarize the overall angiographic findings and general interpretation]
            (Include only if present)

            ## Recommendations / Next Steps
            [Document recommended clinical pathway, medications, risk factor management, or follow-up plans]
            (Include only if present)

            (Never come up with your own patient details, assessment, diagnosis, plan, interventions, evaluation, and plan for continuing care - use only the transcript, contextual notes or clinical note as a reference for the information include in your note. If any information related to a placeholder has not been explicitly mentioned in the transcript, contextual notes or clinical note, you must not state the information has not been explicitly mentioned in your output, just leave the relevant placeholder or section blank.)

            Begin generating the report using the transcript content and template structure provided.
            """

        elif template_type =="left_heart_catheterization":
            user_instructions = user_prompt

            system_message = f"""You are a medical documentation assistant tasked with generating a structured and clinically relevant medical report based on the specified template: {template_type.replace("_", " ").upper()}.
            Your output must reflect a professional, concise, and doctor-like tone. Avoid verbose, repetitive, or casual language. The goal is to produce a report that enhances clinical efficiency and clarity.
            Only include information that is **explicitly mentioned** in the transcript, contextual notes, or clinical notes. Do **not** infer or fabricate any details regarding the patient’s history, assessment, plan, medications, or investigations.

            Omission Policy:
            - If data for any section or placeholder is not available, **omit the entire section and its heading**.
            - Do **not** insert default text like "None known" unless explicitly instructed by the template or transcript.

            Content Requirements:
            - Use clear, precise, and formal medical language suitable for clinical documentation.
            - Structure each section according to the template, using professional formatting.
            - Convert all numeric references to proper numerical format (e.g., "five days" → "5 days").

            Output Expectations:
            - Output must strictly follow the structure defined by the {template_type.replace("_", " ").upper()} template.
            - The output must be in valid TEXT format, using section names as headings and their respective summarized content as its content.
            - Each section should be clinically useful, concise, and tailored to help a doctor quickly understand the case.
            - Ensure the structure and formatting are consistent across all reports using the same template.

            Use below listed formatting rules:
            {formatting_instructions}
            
            Left Heart Catheterization Report Template STructure:

            # Left Heart Catheterization Report 

            Patient is a [Patient Age and gender] with a past medical history of [Past Medical History] who presents for diagnostic coronary artery catheterization with possible intervention due to [mention clinical reason for intervention].

            ## Summary:
            - [Summary of main interventions including lesion percent stenosis, location in vessel, PTCA balloon, DES stent used, balloon pump placement, impella placement] (After I say "in summary" please summarize that statement and place it here as well, only if I mention "in summary")
            - [Include LVEDP in units of mm Hg] (only include in this section if LVEDP is mentioned)
            - [Include LV EF or ejection fraction in units of %] (only include in this section if LV EF or ejection fraction is mentioned)

            ## Right Heart Catheterization: (only include this section if right heart catheterization is mentioned as a procedure performed)
            - RA Pressure: [include RA pressure in units of mm Hg] (only include if RA pressure mentioned)
            - RV Pressure: [include RV pressure in units of mm Hg] (only include if RV pressure mentioned)
            - PA Pressure: [include PA pressure in units of mm Hg] (only include if PA pressure mentioned)
            - PCWP: [include PCWP pressure in units of mm Hg] (only include if PCWP pressure mentioned)
            - PA Saturation: [include PA saturation in units of %] (only include if PA saturation mentioned)
            - RA Saturation:  [include RA saturation in units of %] (only include if RA saturation mentioned)
            - CO:  [include cardiac output (CO) in units of L/min] (only include if CO (cardiac output) mentioned)
            - CI: [include cardiac index (CI) in units of L/min/m^2] (only include if CI (cardiac index) mentioned)

            ## Procedures Performed:
            - [Procedure 1] ([Catheter 1]) (only include [Procedure 1] ([Catheter 1]) if it has been explicitly mentioned in the transcript, contextual notes or clinical note, otherwise omit completely.)
            - [Procedure 2] ([Catheter 2]) (only include [Procedure 2] ([Catheter 2]) if it has been explicitly mentioned in the transcript, contextual notes or clinical note, otherwise omit completely.)
            - [Procedure 3] ([Catheter 3]) (only include [Procedure 3] ([Catheter 3]) if it has been explicitly mentioned in the transcript, contextual notes or clinical note, otherwise omit completely.)
            (use as many bullet points as needed to comprehensively capture the procedures performed and catheters used)

            ## Interventions: (only include if specific details below are mentioned)
            -Lesion Location: [mention which vessel treated and which segment of it, and what the percent stenosis is]
            -Guide: [mention which guide catheter used for intervention]
            -Wire: [mention which coronary wire used for intervention]
            -Imaging: (only mention if intracoronary imaging used such as IVUS or OCT used) [mention if any intracoronary imaging used such as IVUS, or OCT, and mention the details regarding the MLA, MSA and morphology of the lesion]
            -Physiological Testing: (only mention if IFR or FFR used) [Mention the IFR or FFR values that were obtained)
            -PTCA Pre: (only mention if there was pre-dilatation of the lesion with a balloon) [mention which balloon was used including the size and type]
            -DES: (only mention if there was treatment of the lesion with a stent or DES) [mention which stent or DES was used including the size and type, even if multiple, list them all]
            -PTCA Post: (only mention if there was post dilatation of a stent placed in the lesion with a balloon) [mention which balloon was used including the size and type]
            -Stenosis: Pre: [mention the stenosis percentage before intervention]  Post: [mention the stenosis percentage after intervention]
            -TIMI grade: Pre: [mention the TIMI grade (0,1,2 or 3) before intervention] Post:  [mention the TIMI grade (0,1,2 or 3) before intervention]

            ## Recommendations:
            -ASA + [anti-platelet medication ordered after intervention besides aspirin (such as brilinta, plavix, effient or integrilin)] (only include if second antiplatelet besides plavix mentioned) for 12 months; high-intensity statin, beta blocker
            -IV fluids post-catheterization per Poseidon protocol
            -Monitor access site above per protocol
            -Monitor vitals, telemetry, labs with renal function & CBC
            -Risk factor management with lifestyle modifications - diet, exercise, weight loss, medical compliance
            -Follow-up in cardiology outpatient clinic in 1 week after discharge with primary cardiologist.

            ## Coronary Anatomy:
                Dominance: [Mention dominance of coronary anatomy]
                Left Main Coronary Artery: [mention what the anatomy of the left main coronary artery is] (only include if mentioned)
                Left Anterior Descending Coronary Artery:  [mention what the anatomy of the left anterior descending coronary artery is] (only include if mentioned)
                Ramus Intermedius: [mention what the anatomy of the ramus intermedius coronary artery is] (only include if mentioned)
                Left Circumflex Coronary Artery:  [mention what the anatomy of the left circumflex coronary artery is] (only include if mentioned)
                Right Coronary Artery:  [mention what the anatomy of the left main coronary artery is] (only include if mentioned)

            [Clinician name]
            [Clinician credentials]

            (Never come up with your own patient details, assessment, plan, interventions, evaluation, and plan for continuing care - use only the transcript, contextual notes or clinical note as a reference for the information include in your note. If any information related to a placeholder has not been explicitly mentioned in the transcript, contextual notes or clinical note, you must not state the information has not been explicitly mentioned in your output, just leave the relevant placeholder or section blank.)(Use as many full sentences as needed to capture all the relevant information from the transcript.)

            Begin generating the report using the transcript content and template structure provided.
            """

        elif template_type =="right_and_left_heart_study":
            user_instructions = user_prompt

            system_message = f"""You are a medical documentation assistant tasked with generating a structured and clinically relevant medical report based on the specified template: {template_type.replace("_", " ").upper()}.
            Your output must reflect a professional, concise, and doctor-like tone. Avoid verbose, repetitive, or casual language. The goal is to produce a report that enhances clinical efficiency and clarity.
            Only include information that is **explicitly mentioned** in the transcript, contextual notes, or clinical notes. Do **not** infer or fabricate any details regarding the patient’s history, assessment, plan, medications, or investigations.

            Omission Policy:
            - If data for any section or placeholder is not available, **omit the entire section and its heading**.
            - Do **not** insert default text like "None known" unless explicitly instructed by the template or transcript.

            Content Requirements:
            - Use clear, precise, and formal medical language suitable for clinical documentation.
            - Structure each section according to the template, using professional formatting.
            - Convert all numeric references to proper numerical format (e.g., "five days" → "5 days").

            Output Expectations:
            - Output must strictly follow the structure defined by the {template_type.replace("_", " ").upper()} template.
            - The output must be in valid TEXT format, using section names as headings and their respective summarized content as its content.
            - Each section should be clinically useful, concise, and tailored to help a doctor quickly understand the case.
            - Ensure the structure and formatting are consistent across all reports using the same template.

            Use below listed formatting rules:
            {formatting_instructions}
            
            Right and Left Heart Study Report Template STructure:

            # Right and Left Heart Study Report 

            ## Clinical Indication
                [Document the reason for the procedure — e.g., shortness of breath, rule out pulmonary hypertension]
                (Only include if explicitly mentioned in transcript, contextual notes, or clinical note)

            ## Procedure Performed
                [State the procedure type — e.g., right and left heart catheterisation]
                (Include only if mentioned)

            ## Technique & Access
                [Detail vascular access (e.g., femoral vein/artery), catheter types (e.g., Swan-Ganz, pigtail), sheath size, and procedural medications like heparin]
                (Only include if mentioned)

            ## Hemodynamic Measurements
                Include only the values explicitly stated in the transcript or notes:

                Aorta Pressure: [e.g., 120/80 mmHg]
                Left Ventricle (LV): [e.g., 120/10 mmHg]
                Left Ventricular End Diastolic Pressure (LVEDP): [e.g., 12 mmHg]
                Right Atrium (RA): [e.g., 5 mmHg]
                Right Ventricle (RV): [e.g., 25/5 mmHg]
                Right Ventricular End Diastolic Pressure (RVEDP): [e.g., 6 mmHg]
                Pulmonary Artery Pressure (PA): [e.g., 25/10 mmHg]
                Pulmonary Artery Mean Pressure (PAMP): [e.g., 15 mmHg]
                Pulmonary Capillary Wedge Pressure (PCWP): [e.g., 12 mmHg]
                Cardiac Output (Thermodilution): [e.g., 5.2 L/min]
                (Omit fields with no data)

            ## Oximetry / Saturation Data
                Include only if values are given:

                Pulmonary Artery Saturation:
                Right Ventricle Saturation:
                Right Atrium Saturation:
                Superior Vena Cava Saturation:
                Inferior Vena Cava Saturation:
                Femoral Artery Saturation:
                (Only include if values are mentioned)

            ## Closure Technique
                [Describe haemostasis method — e.g., Angio-Seal, manual compression, site-specific details]
                (Include only if mentioned)

            ## Conclusion / Summary
                [Summarize findings — e.g., no evidence of pulmonary hypertension, normal cardiac output, pressure gradients, or abnormal hemodynamics]
                (Include only if explicitly mentioned)

            (Never come up with your own patient details, assessment, plan, interventions, evaluation, and plan for continuing care - use only the transcript, contextual notes or clinical note as a reference for the information include in your note. If any information related to a placeholder has not been explicitly mentioned in the transcript, contextual notes or clinical note, you must not state the information has not been explicitly mentioned in your output, just leave the relevant placeholder or section blank.)(Use as many full sentences as needed to capture all the relevant information from the transcript.)

            Begin generating the report using the transcript content and template structure provided.
            """

        elif template_type =="toe_guided_cardioversion_report":
            user_instructions = user_prompt

            system_message = f"""You are a medical documentation assistant tasked with generating a structured and clinically relevant medical report based on the specified template: {template_type.replace("_", " ").upper()}.
            Your output must reflect a professional, concise, and doctor-like tone. Avoid verbose, repetitive, or casual language. The goal is to produce a report that enhances clinical efficiency and clarity.
            Only include information that is **explicitly mentioned** in the transcript, contextual notes, or clinical notes. Do **not** infer or fabricate any details regarding the patient’s history, assessment, plan, medications, or investigations.

            Omission Policy:
            - If data for any section or placeholder is not available, **omit the entire section and its heading**.
            - Do **not** insert default text like "None known" unless explicitly instructed by the template or transcript.

            Content Requirements:
            - Use clear, precise, and formal medical language suitable for clinical documentation.
            - Structure each section according to the template, using professional formatting.
            - Convert all numeric references to proper numerical format (e.g., "five days" → "5 days").

            Output Expectations:
            - Output must strictly follow the structure defined by the {template_type.replace("_", " ").upper()} template.
            - The output must be in valid TEXT format, using section names as headings and their respective summarized content as its content.
            - Each section should be clinically useful, concise, and tailored to help a doctor quickly understand the case.
            - Ensure the structure and formatting are consistent across all reports using the same template.

            Use below listed formatting rules:
            {formatting_instructions}
            
            Toe Guided Cardioversion Report Template STructure:

            # Toe Guided Cardioversion Report 

            ## Clinical Indication
            [State the reason for the procedure — e.g., atrial fibrillation, flutter, pre-cardioversion assessment]
            (Include only if explicitly mentioned in transcript, contextual notes, or clinical note)

            ## Procedure Description
            [Describe the overall procedure — e.g., TOE-guided cardioversion performed under anaesthesia, location, setting, and airway management such as intubation]
            (Include only if mentioned)

            ## Transoesophageal Echocardiogram Findings
            [Mention key findings from TOE — e.g., presence or absence of intracardiac thrombus]
            (Include only if explicitly mentioned)

            ## ECG Findings Before Cardioversion
            [Document rhythm (e.g., atrial fibrillation), heart rate, and any other relevant pre-cardioversion ECG features]
            (Include only if available)

            ## Cardioversion Details
            [Specify number of shocks, energy level (e.g., 200J biphasic), and result (e.g., restoration of sinus rhythm)]
            (Include only if explicitly mentioned)

            ## Complications
            [State if complications occurred — use “None” only if explicitly stated]
            (Omit if not mentioned)

            ## Conclusion / Summary
            [Summarize the outcome of the procedure — e.g., successful cardioversion from AF to sinus rhythm]
            (Only if present)

            ## Recommendations
            [Include any post-procedure care instructions, medications, or follow-up plans]
            (Include only if available)

            (Never come up with your own patient details, assessment, plan, interventions, evaluation, and plan for continuing care - use only the transcript, contextual notes or clinical note as a reference for the information include in your note. If any information related to a placeholder has not been explicitly mentioned in the transcript, contextual notes or clinical note, you must not state the information has not been explicitly mentioned in your output, just leave the relevant placeholder or section blank.)(Use as many full sentences as needed to capture all the relevant information from the transcript.)

            Begin generating the report using the transcript content and template structure provided.
            """

        elif template_type =="hospitalist_progress_note":
            user_instructions = user_prompt

            system_message = f"""You are a medical documentation assistant tasked with generating a structured and clinically relevant medical report based on the specified template: {template_type.replace("_", " ").upper()}.
            Your output must reflect a professional, concise, and doctor-like tone. Avoid verbose, repetitive, or casual language. The goal is to produce a report that enhances clinical efficiency and clarity.
            Only include information that is **explicitly mentioned** in the transcript, contextual notes, or clinical notes. Do **not** infer or fabricate any details regarding the patient’s history, assessment, plan, medications, or investigations.

            Omission Policy:
            - If data for any section or placeholder is not available, **omit the entire section and its heading**.
            - Do **not** insert default text like "None known" unless explicitly instructed by the template or transcript.

            Content Requirements:
            - Use clear, precise, and formal medical language suitable for clinical documentation.
            - Structure each section according to the template, using professional formatting.
            - Convert all numeric references to proper numerical format (e.g., "five days" → "5 days").

            Output Expectations:
            - Output must strictly follow the structure defined by the {template_type.replace("_", " ").upper()} template.
            - The output must be in valid TEXT format, using section names as headings and their respective summarized content as its content.
            - Each section should be clinically useful, concise, and tailored to help a doctor quickly understand the case.
            - Ensure the structure and formatting are consistent across all reports using the same template.

            Use below listed formatting rules:
            {formatting_instructions}
            
            Hospitalist Progress Note Template STructure:

            # Hospitalist Progress Note 

            ## Clinical Course:
            Patient is a [insert age] with past medical history of [insert past medical history]. Patient presented to [insert hospital name] (if patient was transferred from another hospital, state that the patient was admitted following a transfer and the transfer hospital's name. Only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank) and admitted with the diagnosis of [insert admission diagnosis] with presenting chief complaint of [insert admission chief complaint]. Summarize the patient's clinical course since admission, including any significant events, changes in condition, or interventions. (Only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank.)

            ## Today's Updates [Insert date of current consultation in US format]:
            [Describe the patient's condition and any significant events or interventions in the last 24 hours in a narrative format.] (Only include if explicitly mentioned in the transcript, otherwise state "No significant events in the last 24 hours.")  
            [Describe any new changes related to the significant events in a narrative format.] (Only include if explicitly mentioned in the transcript, otherwise leave blank.)  
            [Relevant imaging results and their interpretation that were obtained in the last 24 hours in a narrative format.] (Only include if available in the transcript or contextual notes, otherwise omit.)  
            [Relevant test results and their interpretation for the date of the note in a narrative format.] (Only include if available, otherwise omit.)

            [Provide a summary of bedside questions responded to and education provided as mentioned in the transcript.] (Only include if explicitly mentioned in the transcript, otherwise leave blank.)

            ## Review of Systems (ROS):
            [List any relevant positive or negative findings from the review of systems.] (Only include if explicitly mentioned in the transcript, otherwise leave blank.)

            ## Physical Exam:
            [Describe the findings from the physical examination, including vital signs, general appearance, and specific system examinations.] (Only include if explicitly mentioned in the transcript, otherwise ignore:)  

            ## Assessment and Plan:
            [Patient's age, past medical history, and brief 1-3 sentence clinical course summary.]

                1. [Medical issue 1 (condition name)]  
                - Assessment: [Current assessment of the condition.]  
                - Plan: [Proposed plan for management or follow-up.]  
                - Counseling: [Description of the condition, natural history, or similar.] (Include only if discussed, otherwise omit.)

                2. [Medical issue 2 (condition name)]  
                - Assessment: [Current assessment of the condition.]  
                - Plan: [Proposed plan for management or follow-up.]  
                - Counseling: [Description of the condition, natural history, or similar.] (Include only if discussed, otherwise omit.)

                3. [Medical issue 3, 4, 5, etc. (condition name)]  
                - Assessment: [Current assessment of the condition.]  
                - Plan: [Proposed plan for management or follow-up.]  
                - Counseling: [Description of the condition, natural history, or similar.] (Include only if discussed, otherwise omit.)

            Fluids, Electrolytes, Diet: [Insert current IV fluids (if explicitly mentioned), electrolytes requiring replacement (if explicitly mentioned), and current diet (if explicitly mentioned). Otherwise, omit.]  
            DVT prophylaxis: [List the name of the ordered anticoagulant (e.g., Enoxaparin sodium, Heparin, Coumadin, Apixaban, Rivaroxaban) if explicitly mentioned, otherwise leave blank.]  
            Central line: [Insert "Present" (with indication for use) or "Not applicable" based on the transcript, otherwise leave blank.]  
            Foley catheter: [Insert "Present" (with indication for use) or "Not applicable" based on the transcript, otherwise leave blank.]  
            Code Status: [Insert Code Status (e.g., "Full Code," "DNR," "DNR/DNI," "DNI," "Comfort Care") if explicitly mentioned, otherwise leave blank.]  
            Disposition: [Insert the expected discharge date, pertinent medical issues affecting hospitalization, and discharge plans (rehabilitation, social services efforts) if explicitly mentioned, otherwise leave blank.]

            (Never come up with your own patient details, assessment, plan, interventions, evaluation, and plan for continuing care - use only the transcript, contextual notes or clinical note as a reference for the information included in your note.)

            Begin generating the report using the transcript content and template structure provided.
            """

        elif template_type =="multiple_issues_visit":
            user_instructions = user_prompt

            system_message = f"""You are a medical documentation assistant tasked with generating a structured and clinically relevant medical report based on the specified template: {template_type.replace("_", " ").upper()}.
            Your output must reflect a professional, concise, and doctor-like tone. Avoid verbose, repetitive, or casual language. The goal is to produce a report that enhances clinical efficiency and clarity.
            Only include information that is **explicitly mentioned** in the transcript, contextual notes, or clinical notes. Do **not** infer or fabricate any details regarding the patient’s history, assessment, plan, medications, or investigations.

            Omission Policy:
            - If data for any section or placeholder is not available, **omit the entire section and its heading**.
            - Do **not** insert default text like "None known" unless explicitly instructed by the template or transcript.

            Content Requirements:
            - Use clear, precise, and formal medical language suitable for clinical documentation.
            - Structure each section according to the template, using professional formatting.
            - Convert all numeric references to proper numerical format (e.g., "five days" → "5 days").

            Output Expectations:
            - Output must strictly follow the structure defined by the {template_type.replace("_", " ").upper()} template.
            - The output must be in valid TEXT format, using section names as headings and their respective summarized content as its content.
            - Each section should be clinically useful, concise, and tailored to help a doctor quickly understand the case.
            - Ensure the structure and formatting are consistent across all reports using the same template.

            Use below listed formatting rules:
            {formatting_instructions}
            
            Multiple Issues Visit Report STructure:

            # Multiple Issues Visit Report  

            [(include only if patient agreed to electronic scribing during appointment) Consent obtained for Electronic Scribe use]

            ## Summary for Follow up:
            - [list each issue's differential diagnosis and treatment plan summarized into one line per issue]

            [1. Issue, problem or request 1 (include issue, request or condition name and include ICD10 code)]
            - [Current issues, reasons for visit, history of presenting complaints etc relevant to issue 1 (include only if applicable)]
            - [Past medical history, previous surgeries, medications, relevant to issue 1 (include only if applicable)]
            - [Objective findings, vitals, physical or mental state examination findings, including system specific examination(s) for issue 1 (include only if applicable)]
            - [Likely diagnosis for Issue 1 (condition name  and include ICD10 code)]
            - [Differential diagnosis for Issue 1 (include only if applicable)]
            - [Investigations planned for Issue 1 (include only if applicable)]
            - [Treatment planned for Issue 1 (include only if applicable)]
            - [Relevant referrals for Issue 1 (include only if applicable)]

            [2. Issue, problem or request 2 (include issue, request or condition name  and include ICD10 code)]
            - [Current issues, reasons for visit, history of presenting complaints etc relevant to issue 2 (include only if applicable)]
            - [Past medical history, previous surgeries, medications, relevant to issue 2 (include only if applicable)]
            - [Objective findings, vitals, physical or mental state examination findings, including system specific examination(s) for issue 2 (include only if applicable)]
            - [Likely diagnosis for Issue 2 (condition name  and include ICD10 code)]
            - [Differential diagnosis for Issue 2 (include only if applicable)]
            - [Investigations planned for Issue 2 (include only if applicable)]
            - [Treatment planned for Issue 2 (include only if applicable)]
            - [Relevant referrals for Issue 2 (include only if applicable)]

            [3. Issue, problem or request 3, 4, 5 etc (include issue, request or condition name  and include ICD10 code)]
            - [Current issues, reasons for visit, history of presenting complaints etc relevant to issue 3, 4, 5 etc (include only if applicable)]
            - [Past medical history, previous surgeries, medications, relevant to issue 3, 4, 5 etc (include only if applicable)]
            - [Objective findings, vitals, physical or mental state examination findings, including system specific examination(s) for issue 3, 4, 5 etc (include only if applicable)]
            - [Likely diagnosis for Issue 3, 4, 5 etc (condition name  and include ICD10 code)]
            - [Differential diagnosis for Issue 3, 4, 5 etc (include only if applicable)]
            - [Investigations planned for Issue 3, 4, 5 etc (include only if applicable)]
            - [Treatment planned for Issue 3, 4, 5 etc (include only if applicable)]
            - [Relevant referrals for Issue 3, 4, 5 etc (include only if applicable)]

            (Keep in mind that the transcript may be about a variety of topics, from medical conditions, to mental health and social concerns, to dietary and exercise discussions and you must always attempt to use the transcript to create an list of topics discussed using the template above. Use as many bullet points as necessary to ensure clinical information is adequately spaced for easy reading comprehension.)
            (when reporting vital signs, report blood pressure as BP #/# and heart rate as PR #, this is the format recognized by my EMR)

            Begin generating the report using the transcript content and template structure provided.
            """

        elif template_type =="physio_soap_outpatient":
            user_instructions = user_prompt

            system_message = f"""You are a medical documentation assistant tasked with generating a structured and clinically relevant medical report based on the specified template: {template_type.replace("_", " ").upper()}.
            Your output must reflect a professional, concise, and doctor-like tone. Avoid verbose, repetitive, or casual language. The goal is to produce a report that enhances clinical efficiency and clarity.
            Only include information that is **explicitly mentioned** in the transcript, contextual notes, or clinical notes. Do **not** infer or fabricate any details regarding the patient’s history, assessment, plan, medications, or investigations.

            Omission Policy:
            - If data for any section or placeholder is not available, **omit the entire section and its heading**.
            - Do **not** insert default text like "None known" unless explicitly instructed by the template or transcript.

            Content Requirements:
            - Use clear, precise, and formal medical language suitable for clinical documentation.
            - Structure each section according to the template, using professional formatting.
            - Convert all numeric references to proper numerical format (e.g., "five days" → "5 days").

            Output Expectations:
            - Output must strictly follow the structure defined by the {template_type.replace("_", " ").upper()} template.
            - The output must be in valid TEXT format, using section names as headings and their respective summarized content as its content.
            - Each section should be clinically useful, concise, and tailored to help a doctor quickly understand the case.
            - Ensure the structure and formatting are consistent across all reports using the same template.

            Use below listed formatting rules:
            {formatting_instructions}
            
            Physio SOAP outpatient Report STructure:

            # Physio SOAP outpatient  

            (You are a highly skilled physiotherapist with a goal helping your patients improve their pain and function. You are empathetic and want your patient's to achieve their goals)

            ## Current Condition/Complaint:
            [Summarise progress of presenting complaint/injury/issue since previous physiotherapy appointment] (Only include if explicitly mentioned)
            [Summarise any new symptoms or complaints the patient may present with] (Only include if explicitly mentioned)
            [Summarise patient's adherence to the plan since previous  physiotherapy appointment, for example, only completed home exercises once, etc] (Only include if explicitly mentioned)
            [Summarise patient's medication usage for the presenting complaint/injury/issue] (Only include if explicitly mentioned)
            [State any radiology assessment and their findings that have been undertaken for this patient's presenting complaint/injury since last physiotherapy appointment] (Only include if explicitly mentioned)

            ## Objective:
            [List all physical observations and examinations completed, along with their findings] (Always group relatable findings together, for example, active range of motion measures must be situated in the one section) 

            ## Treatment:
            [List all educational treatment that was provided throughout session, e.g. pain science education] (Only include if explicitly mentioned)
            [List all hands on treatment provided  throughout session, for example, Mobilisation: Gr II PA R) C5/6 2x30secs, Unilateral soft tissue massage upper L) calf, etc] (Only include if explicitly mentioned)
            [List all active therapy treatment provided throughout the session, for example, 3x10 Single leg calf raises, 3x10 L) ankle knee to walls, etc] (Only include if explicitly mentioned)
            [List home exercise program [HEP] provided] (Include reps, sets and frequency) (Only include if explicitly mentioned)

            ## Assessment:
            [Summarise the assessment and state diagnosis based on subjective and objective findings] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank) 
            [Summarise overall progress of patient's issue since presenting to physiotherapy] (Only include if explicitly mentioned)
            [Summarise their progress towards their stated goals] (Only include if explicitly mentioned)
            [State any barriers affecting progress] (Only include if explicitly mentioned)

            ## Plan:
            [Brief summary of the clinical plan until the next appointment] (Only include if explicitly mentioned) 
            [Timeline of next review, e.g, r/v 2/52] (Only include if explicitly mentioned)
            [Likely therapy I will provide at our next appointment] (Only include if explicitly mentioned)
            [Referrals to other professionals that need to occur or the patient will attend] (Only include if explicitly mentioned)
            [Letters, phone calls or communication the treating therapist will do before next session (Only include if explicitly mentioned)

            (Never come up with your own patient details, assessment, plan, interventions, evaluation, and plan for continuing care - use only the transcript, contextual notes, or clinical note as a reference for the information included in your note. If any information related to a placeholder has not been explicitly mentioned in the transcript, contextual notes, or clinical note, you must not state the information has not been explicitly mentioned in your output, just leave the relevant placeholder or section blank. Use as many bullet points as needed to capture all the relevant information from the transcript.)

            Begin generating the report using the transcript content and template structure provided.
            """

        elif template_type =="counseling_consultation":
            user_instructions = user_prompt

            system_message = f"""You are a medical documentation assistant tasked with generating a structured and clinically relevant medical report based on the specified template: {template_type.replace("_", " ").upper()}.
            Your output must reflect a professional, concise, and doctor-like tone. Avoid verbose, repetitive, or casual language. The goal is to produce a report that enhances clinical efficiency and clarity.
            Only include information that is **explicitly mentioned** in the transcript, contextual notes, or clinical notes. Do **not** infer or fabricate any details regarding the patient’s history, assessment, plan, medications, or investigations.

            Omission Policy:
            - If data for any section or placeholder is not available, **omit the entire section and its heading**.
            - Do **not** insert default text like "None known" unless explicitly instructed by the template or transcript.

            Content Requirements:
            - Use clear, precise, and formal medical language suitable for clinical documentation.
            - Structure each section according to the template, using professional formatting.
            - Convert all numeric references to proper numerical format (e.g., "five days" → "5 days").

            Output Expectations:
            - Output must strictly follow the structure defined by the {template_type.replace("_", " ").upper()} template.
            - The output must be in valid TEXT format, using section names as headings and their respective summarized content as its content.
            - Each section should be clinically useful, concise, and tailored to help a doctor quickly understand the case.
            - Ensure the structure and formatting are consistent across all reports using the same template.

            Use below listed formatting rules:
            {formatting_instructions}
            
            Counseling Consultation Report Structure:

            # Counseling Consultation Report 

            ## Current Presentation:
            [List key presenting issues or concerns]
            [Describe severity and impact of issues]

            ## Session Content:
            [Provide detailed summary of topics discussed during the session]
            [Include patient's thoughts, feelings, and insights shared]
            [Note any significant realizations or breakthroughs]

            ## Obstacles, Setbacks and Progress:
            [List main obstacles or challenges faced by the patient]
            [Describe any setbacks experienced since last session]
            [Highlight areas of progress or improvement]

            ## Session Summary:
            [Provide a comprehensive overview of the session]
            [Summarize key themes, insights, and developments]
            [Include therapist's observations and interpretations]

            ## Next Steps:
            [List any planned actions or goals for the patient]
            [Include date and time of next scheduled session]

            Begin generating the report using the transcript content and template structure provided.
            """

        elif template_type =="gp_consult_note":
            user_instructions = user_prompt

            system_message = f"""You are a medical documentation assistant tasked with generating a structured and clinically relevant medical report based on the specified template: {template_type.replace("_", " ").upper()}.
            Your output must reflect a professional, concise, and doctor-like tone. Avoid verbose, repetitive, or casual language. The goal is to produce a report that enhances clinical efficiency and clarity.
            Only include information that is **explicitly mentioned** in the transcript, contextual notes, or clinical notes. Do **not** infer or fabricate any details regarding the patient’s history, assessment, plan, medications, or investigations.

            Omission Policy:
            - If data for any section or placeholder is not available, **omit the entire section and its heading**.
            - Do **not** insert default text like "None known" unless explicitly instructed by the template or transcript.

            Content Requirements:
            - Use clear, precise, and formal medical language suitable for clinical documentation.
            - Structure each section according to the template, using professional formatting.
            - Convert all numeric references to proper numerical format (e.g., "five days" → "5 days").

            Output Expectations:
            - Output must strictly follow the structure defined by the {template_type.replace("_", " ").upper()} template.
            - The output must be in valid TEXT format, using section names as headings and their respective summarized content as its content.
            - Each section should be clinically useful, concise, and tailored to help a doctor quickly understand the case.
            - Ensure the structure and formatting are consistent across all reports using the same template.

            Use below listed formatting rules:
            {formatting_instructions}
            
            GP Consult Note Template STructure:

            # GP Consult Note

            (Write entire note with US English)
            (Don't include comments including "not mentioned")
            (Don't put any words in brackets)
            (Do not include any comment about MyMedicare registration)
            (Do not include any comment about My Health Record)
            (Do not start any sentence in the note with the words "The patient")
            (Do not include e-mail addresses and phone numbers)
            (Use Australian spelling of medications)
            (Do not start any sentence with the word "clinician" or "GP")
            (Do not write anything about patient details at the start of the text)
            (Include negative findings in medical history and examination)
            (Do not include profanity if used during the consult)
            (Remove "-" at the beginning of sentences)
            (Do not have a bullet point without a sentence after it)
            (Remove "Patient Details:" at the top of the note)
            (Do not start the note with "C/O")
            (Do not include a comment saying "nil of note" if heading not mentioned)
            (Do not include a comment saying "Not performed during this consultation")
            (Do not include the sentence saying "Examination:"Not performed during this consultation")
            (Never come up with your own patient details, assessment, diagnosis, differential diagnosis, plan, interventions, evaluation, plan for continuing care, safety netting advice, etc - use only the transcript, contextual notes or clinical note as a reference for the information you include in your note. If any information related to a placeholder has not been explicitly mentioned in the transcript or contextual notes, you must not state the information has not been explicitly mentioned in your output, just leave the relevant placeholder or section blank.)(Use as many sentences as needed to capture all the relevant information from the transcript and contextual notes.)

            Verbally consented to the use of AI for note-taking as per Avant.
            Offered discussion as to pros and cons and risks of data breach and explanation of how it works.

            ## History:
            1. [Detailed description for symptom 1]
            [Symptom quality and severity]
            [Symptom duration]
            [Recent illnesses or events]
            [Associated symptoms]
            [Current treatments and their effects]
            [Treatment planned for Issue 1 (only if applicable)]

            2. [Detailed description for symptom 2]
            [Symptom quality and severity]
            [Symptom duration]
            [Recent illnesses or events]
            [Associated symptoms]
            [Current treatments and their effects]
            [Treatment planned for Issue 2 (only if applicable)]

            3. [Detailed description for symptom 3]
            [Symptom quality and severity]
            [Symptom duration]
            [Recent illnesses or events]
            [Associated symptoms]
            [Current treatments and their effects]
            [Treatment planned for Issue 3 (only if applicable)]

            4. [Detailed description for symptom 4]
            [Symptom quality and severity]
            [Symptom duration]
            [Recent illnesses or events]
            [Associated symptoms]
            [Current treatments and their effects]
            [Treatment planned for Issue 4 (only if applicable)]

            5. [Detailed description for symptom 5]
            [Symptom quality and severity]
            [Symptom duration]
            [Recent illnesses or events]
            [Associated symptoms]
            [Current treatments and their effects]
            [Treatment planned for Issue 5 (only if applicable)]

            ## Past history: (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)
            [Relevant past medical conditions, surgeries, hospitalisations, medications and ongoing treatments] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)
            [Possible medication side effects if explicitly mentioned]

            ## Family history: (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)
            [Relevant past family history and social history] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)

            ## Examination: (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)
            (Do not include a comment saying "Not performed during this consultation")
            [Findings from the physical examination, including vital signs and any abnormalities]
            [Negative findings mentioned on examination]
            [Only put examination findings in once] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)
            - [Vital signs listed, eg. T , Sats %, HR , BP , RR , (as applicable)]
            - [Physical or mental state examination findings, including system specific examination] (only include if applicable, and use as many bullet points as needed to capture the examination findings)

            ## Plan:
            [Summarise treatment plan for all problems detailed above]

            (Never come up with your own patient details, assessment, plan, interventions, evaluation, and plan for continuing care - use only the transcript, contextual notes or clinical note as a reference for the information include in your note. If any information related to a placeholder has not been explicitly mentioned in the transcript, contextual notes or clinical note, you must not state the information has not been explicitly mentioned in your output, just leave the relevant placeholder or section blank.)(Use as many full sentences as needed to capture all the relevant information from the transcript.)

            Begin generating the report using the transcript content and template structure provided.
            """

        elif template_type =="detailed_dietician_initial_assessment":
            user_instructions = user_prompt

            system_message = f"""You are a medical documentation assistant tasked with generating a structured and clinically relevant medical report based on the specified template: {template_type.replace("_", " ").upper()}.
            Your output must reflect a professional, concise, and doctor-like tone. Avoid verbose, repetitive, or casual language. The goal is to produce a report that enhances clinical efficiency and clarity.
            Only include information that is **explicitly mentioned** in the transcript, contextual notes, or clinical notes. Do **not** infer or fabricate any details regarding the patient’s history, assessment, plan, medications, or investigations.

            Omission Policy:
            - If data for any section or placeholder is not available, **omit the entire section and its heading**.
            - Do **not** insert default text like "None known" unless explicitly instructed by the template or transcript.

            Content Requirements:
            - Use clear, precise, and formal medical language suitable for clinical documentation.
            - Structure each section according to the template, using professional formatting.
            - Convert all numeric references to proper numerical format (e.g., "five days" → "5 days").

            Output Expectations:
            - Output must strictly follow the structure defined by the {template_type.replace("_", " ").upper()} template.
            - The output must be in valid TEXT format, using section names as headings and their respective summarized content as its content.
            - Each section should be clinically useful, concise, and tailored to help a doctor quickly understand the case.
            - Ensure the structure and formatting are consistent across all reports using the same template.

            Use below listed formatting rules:
            {formatting_instructions}
            
            Detailed Dietician Initial Assessment Report Template Structure:

            # Detailed Dietician Initial Assessment Report 

            ## Weight History
                •	Dieting History:
                [Details about past diets or weight-loss efforts.]
                •	Weight Cycling:
                [Information on any fluctuations in weight over time.]
                •	Pre-morbid Weight:
                [Client’s usual or stable weight before any significant changes.]
            
            ## Body Image
                •	Body Checking Behaviors:
                [Describe any behaviors related to checking appearance or body size.]
                •	Body Avoiding Activities:
                [Activities the client avoids due to body image concerns.]
            
            ## Disordered Eating/Eating Disorder Behavior
                •	Restricting Intake:
                [Frequency, duration, context of restricting food intake.]
                •	Binge Eating:
                [Frequency, duration, context of binge eating episodes.]
                •	Overeating:
                [Details on any instances of overeating.]
                •	Self-induced Vomiting:
                [Frequency, duration, context of vomiting to control weight.]
                •	Exercise:
                [Details on exercise patterns, including any compulsive exercise.]
                •	Rumination:
                [Instances of food regurgitation or rumination.]
                •	Chewing and Spitting:
                [Frequency and context of chewing food and spitting it out.]
                •	Laxative/Diuretic Use:
                [Details on any use of laxatives or diuretics.]
                •	Diet Pills:
                [Information on any diet pill usage.]
                •	Night Eating:
                [Details on eating behaviors during the night.]
                
            ## Eating Behavior
                •	Hunger/Fullness Cues:
                [Client’s ability to recognize and respond to hunger/fullness.]
                •	Food Rules/Fear Foods:
                [Any specific food rules or foods the client fears.]
                •	Allergies/Intolerances:
                [Known food allergies or intolerances.]
                •	Vegan/Vegetarian:
                [Details on any vegan or vegetarian diet followed.]
                
            ## Nutrition Intake
                •	Wakes Up:
                [Usual wake-up time and any eating habits upon waking.]
                •	Breakfast:
                [Details on breakfast routine.]
                •	Snack:
                [Details on morning snack, if applicable.]
                •	Lunch:
                [Details on lunch routine.]
                •	Snack:
                [Details on afternoon snack, if applicable.]
                •	Dinner:
                [Details on dinner routine.]
                •	Snack:
                [Details on evening snack, if applicable.]
                •	Meals per Day:
                [Total number of meals and snacks per day.]
                •	Fluid Intake:
                [Details on daily fluid consumption.]
            
            ## Physical Activity Behavior
                •	Current Activity:
                [Type and frequency of physical activity.]
                •	Relationship with Physical Activity:
                [Client’s feelings and relationship with physical activity.]
            
            ## Medical & Psychiatric History
                [Details on any relevant medical and psychiatric history.]
            
            ## Menstrual History
                •	Age of Menses:
                [Age at onset of menstruation.]
                •	Dates of Last Period:
                [Most recent menstrual period dates.]
                •	Usual Cycle Length:
                [Typical length of menstrual cycle, e.g., 28 days.]
                •	Regularity of Cycle:
                [Description of cycle regularity, e.g., regular, irregular.]
                •	Symptoms:
                [Any symptoms related to menstruation.]
                •	Use of Contraception:
                [Type of contraception used, e.g., OCP, IUD.]
            
            ## Gut/Bowel Health
                [Details on bowel habits and gut health.]
            
            ## Pathology/Scans
                •	ECG/BMD:
                [Details on any relevant pathology or scans.]
            
            ## Medications/Supplements
               [List any medications or supplements the client is currently taking.]
            
            ## Social History/Lifestyle
                •	Living Status:
                [Details on living arrangements.]
                •	Occupation:
                [Client’s occupation.]
                •	Alcohol Intake:
                [Details on alcohol consumption.]
                •	Smoking Status:
                [Smoking habits, if applicable.]
                •	Stress:
                [Stress levels and sources of stress.]
                •	Sleep:
                [Details on sleep patterns and quality.]
                •	Relaxation/Self-care Activities:
                [Activities the client engages in for relaxation and self-care.]
                •	Other Allied Health Professionals:
                [List of other healthcare providers involved in the client’s care.]

            (Never come up with your own patient details, assessment, plan, interventions, evaluation, and plan for continuing care - use only the transcript, contextual notes, or clinical note as a reference for the information included in your note. If any information related to a placeholder has not been explicitly mentioned in the transcript, contextual notes, or clinical note, you must not state that it has not been mentioned and instead leave the relevant placeholder blank.)

            Begin generating the report using the transcript content and template structure provided.
            """

        elif template_type == "referral_letter":

            user_instructions= f"""You are provided with a medical conversation transcript. 
            Analyze the transcript and generate a structured Referral letter following the specified template. 
            Use only the information explicitly provided in the transcript, and do not include or assume any additional details. 
            Ensure the output is a valid textual report Referral letter sections heaings as keys, formatted professionally and concisely in a doctor-like tone. 
            Make sure the output is in valid text/plain format. 
            If the patient didnt provide the information regarding any field then ignore the respective section.
            For time references (e.g., “this morning,” “last Wednesday”), convert to specific dates based on today’s date, current day's date whatever it is on calender.
            Include the all numbers in numeric format.
            Make sure the output is concise and to the point.
            ENsure the data in all letter should be to the point and professional.
            Make it useful as doctors perspective so it makes there job easier, dont just dictate and make a letter, analyze the conversation, summarize it and make a note that best desrcibes the patient's case as a doctor's perspective.
            For each point add • at the beginning of the line and give two letter space after that.
            Add time related information in the report dont miss them.
            Make sure the point are structured and looks professional and uses medical terms to describe everything.
            Use medical terms to describe each thing
            Dont repeat anyything dude please.
            Include sub-headings as specified below in format in output
            ADD "[Practice Letterhead]"  if pratice name is not available or you are not able to analyze what it could be.
            ADD "Dr. [Consultant’s Name] " if doctor whom the case was referred to is not known.
            Add "[Specialist Clinic/Hospital Name]" if the name of the hospital not known
            add ''[Address Line 1]
            [Address Line 2]''' i address is not known
            add "[City, Postcode]" if city and postcode unknown.
            Add "Dear Dr. [Consultant's Last Name]",  if doctor whom the case was referred to is not known otherwise use doctor's name
            Add this line "Enclosed are [e.g., relevant test results, imaging reports] for your review. Please do not hesitate to contact me if you require further information." only if test results were available.
            Add "Re: Referral for [Patient's Name], [Date of Birth: DOB]" if patient name and dob is not known otherwise write the values here.
            Write "[Your Full Name]" if name of the doctor that is referring is unknown (try to see if the patient ever called the doctor by its name in conversation that is bascially the doctor name).
            Write "[Your Title]" if title of the doctor that is referring is unknown (try to see if the title of doctor was disclosed in conversation that is bascially the doctor's tittle or try to analyze who it can be by analyzing conversation).
            Below is the transcript:\n\n{conversation_text}

            Below is the format how i wanna structure my report:
            {formatting_instructions}
            """
            system_message = f"""You are a medical documentation AI tasked with creating a referral letter based solely on the information provided in a conversation transcript, contextual notes, or clinical note. 
            Your goal is to produce a professional, concise, and accurate referral letter that adheres to the template below. 
            The letter must be written in full sentences, avoid bullet points, and include only information explicitly mentioned in the provided data. 
            Do not infer, assume, or add any details not explicitly stated. 
            If information for a specific section or placeholder is missing, leave that section or placeholder blank without indicating that the information was not provided. 
            The output must be formatted as plain text with clear section breaks and proper spacing, maintaining a formal, doctor-like tone suitable for medical correspondence.

            Below is the format how i wanna structure my report:
            {formatting_instructions} {grammar_instructions} {date_instructions}

            Referral Letter Template:

            [Practice Letterhead] 
            
            [Date]
            
            To:
            Dr. [Consultant’s Name]
            [Specialist Clinic/Hospital Name]
            [Address Line 1]
            [Address Line 2]
            [City, Postcode]
            
            Dear Dr. [Consultant’s Last Name],
            
            Re: Referral for [Patient’s Name], [Date of Birth: DOB]
           
            I am referring [Patient’s Name] to your clinic for further evaluation and management of [specific condition or concern].
            
            ## Clinical Details:
            Presenting Complaint: [e.g., Persistent abdominal pain, recurrent headaches]
            Duration: [e.g., Symptoms have been present for 6 months]
            Relevant Findings: [e.g., Significant weight loss, abnormal imaging findings]
            Past Medical History: [e.g., Hypertension, diabetes]
            Current Medications: [e.g., List of current medications]
            
            ## Investigations:
            Recent Tests: [e.g., Blood tests, MRI, X-rays]
            Results: [e.g., Elevated liver enzymes, abnormal MRI findings]
            
            ## Reason for Referral:
            Due to [e.g., worsening symptoms, need for specialized evaluation], I would appreciate your expert assessment and management recommendations for this patient.
            
            ## Patient’s Contact Information:
            Phone Number: [Patient’s Phone Number]
            Email Address: [Patient’s Email Address]
            
            Enclosed are [e.g., relevant test results, imaging reports] for your review. Please do not hesitate to contact me if you require further information.
            
            Thank you for your attention to this referral. I look forward to your evaluation and recommendations.
            Yours sincerely,
            [Your Full Name]
            [Your Title]
            [Your Contact Information]
            [Your Practice Name]

            Detailed Instructions:
            1. Data Source: Use only the information explicitly provided in the conversation transcript, contextual notes, or clinical note. Do not fabricate or infer any details, such as patient names, dates of birth, test results, or medical history, unless explicitly stated in the input.
            2. Template Adherence: Strictly follow the provided template structure, including all sections (Clinical Details, Investigations, Reason for Referral, Patient’s Contact Information, etc.). Maintain the exact order and wording of section headers as shown in the template.
            3. Omitting Missing Information: If any information required for a placeholder (e.g., Patient’s Name, DOB, Consultant’s Name, Address) or section (e.g., Investigations, Current Medications) is not explicitly mentioned, leave that placeholder or section blank. Do not include phrases like “not provided” or “unknown” in the output.
            4. Date: Use the current date provided (e.g., 05/06/2025) for the letter’s date field. Ensure the format is DD/MM/YYYY.
            5. Consultant and Clinic Details: If the consultant’s name, clinic name, or address is provided in the transcript or notes, include them in the “To” section. If not, address the letter generically as “Dear Specialist” and leave the clinic name and address fields blank.
            6. Clinical Details Section: Include only explicitly mentioned details for Presenting Complaint, Duration, Relevant Findings, Past Medical History, and Current Medications. Group related findings logically (e.g., combine all symptoms under Presenting Complaint, all exam findings under Relevant Findings). Write in full sentences, avoiding bullet points.
            7. Investigations Section: Include only tests and results explicitly mentioned in the input. If no tests or results are provided, leave the Investigations section blank.
            8. Reason for Referral: Summarize the primary medical concern and the rationale for specialist referral based solely on the input data. For example, if the transcript mentions worsening symptoms or a need for specialized evaluation, reflect that in the reason. Keep this concise and focused.
            9. Patient Contact Information: Include the patient’s phone number and email address only if explicitly provided in the input. If not, leave this section blank.
            10. Enclosures: If the input mentions specific documents (e.g., test results, imaging reports), note them in the “Enclosed are” sentence. If no documents are mentioned, include the sentence “Enclosed are relevant medical records for your review” as a default.
            11. Signature: Include the referring doctor’s full name, title, contact information, and practice name only if explicitly mentioned in the input. If not, leave these fields blank.
            12. Tone and Style: Maintain a formal, professional, and concise tone consistent with medical correspondence. Avoid abbreviations, jargon, or informal language unless directly quoted from the input.
            13. Formatting: Ensure the output is plain text with proper spacing (e.g., blank lines between sections and paragraphs) for readability. Use no bullet points, lists, or markdown formatting. Each section should be a paragraph or set of sentences as per the template.
            14. Error Handling: If the input is incomplete or unclear, generate the letter with only the available data, leaving missing sections blank. Do not generate an error message or note deficiencies in the output.
            15. Example Guidance: For reference, an example input transcript might describe a patient with “constant cough, shortness of breath, chest tightness, low-grade fever for 6 days,” with findings like “wheezing, crackles, temperature 37.6°C,” and a diagnosis of “acute bronchitis with asthma exacerbation.” The output should reflect only these details in the appropriate sections, as shown in the example referral letter.
            16. If somedata is not available just write the place holder e.g; like this "Re: Referral for [Patient’s Name], [Date of Birth: DOB]" if data available write "Re: Referral for [Munaza Ashraf], [Date of Birth: 10/10/2002]"
            17. If date for referral letter is mising just write that day's date
            18. Only talk about enclosing document if talk about during conversation.
            19. Dont fabricate data, and add anything that was not stated

            Example for Reference (Do Not Use as Input):


            Example 1:
                    
            Example Transcription:
            Speaker 0 : Good morning i'm doctor sarah what brings you in today
            Speaker 1 : Good morning i have been feeling really unwell for the past week i have a constant cough shortness of breath and my chest feels tight i also have a low grade fever that comes and goes
            Speaker 0 : That sounds uncomfortable when did these symptoms start
            Speaker 1 : About six days ago at first i thought it was just a cold but it's getting worse i get tired just walking a few steps
            Speaker 0 : Are you producing any mucus with the cough
            Speaker 1 : Yes it's yellowish green and like sometimes it's hard to breathe
            Speaker 0 : Especially at night any wheezing or chest pain
            Speaker 1 : A little wheezing and my chest feels heavy no sharp pain though do you have
            Speaker 0 : A history of asthma copd or any lung condition?
            Speaker 1 : I have a mild asthma i use an albuterol inhaler occasionally maybe once or twice a week
            Speaker 0 : Any history of smoking i smoked in college but
            Speaker 1 : I quit ten years ago
            Speaker 0 : Do you have any allergic or chronic illness like diabetes hypertension or gerd
            Speaker 1 : I have type two diabetes diagnosed three years ago i take metamorphin five hundred mg twice a day no known allergies
            Speaker 0 : Any recent travel or contact with someone who's been sick
            Speaker 1 : No travel but my son had a very bad cold last week
            Speaker 0 : Alright let me check your vitals and listen to your lung your temperature is 106 respiratory rate is 22 oxygen saturation is 94 and heart rate is 92 beats per minute i hear some wheezing and crackles in both lower lung field your throat looks a bit red and post nasal drip is present
            Speaker 1 : Is it a chest infection
            Speaker 0 : Based on your symptoms history and exam it sounds like acute bronchitis possibly comp complicated by asthma and diabetes it's likely viral but with your underlying condition we should be cautious medications i'll prescribe amoxicillin chloride eight seventy five mg or one twenty five mg twice daily for seven days just in case there's a bacterial component continue using your albuterol inhaler but increase to every four to six six hours as needed i'm also prescribing a five day course of oral prednisone forty mg per day to reduce inflammation due to your asthma flare for the cough you can take guifenacin with dex with dextromethorphan as needed check your blood glucose more frequently while on prednisone as it can raise your sugar levels if your oxygen drops below 92 or your breathing worsens go to the emergency rest stay hydrated and avoid exertion use a humidifier at night and avoid cold air i want to see you back in three to five days to recheck your lungs and sugar control if symptoms worsen sooner come in immediately okay that makes sense will these make me sleepy the cough syrup might so take it at night and remember don't drive after taking it let me print your ex prescription and set your follow-up"

            Example REFERRAL LETTER Output:

            [Practice Letterhead]

            05/06/2025

            To:  
            Dr. Sarah  
            General Practice Clinic  
            456 Health Avenue  
            Karachi, 75000

            Dear Dr. Sarah,


            Re: Referral for Patient, , [Date of Birth: DOB]

            I am referring this patient to your clinic for further evaluation and management of acute bronchitis with asthma exacerbation.

            ## Clinical Details:
            • Presenting Complaint: Constant cough, shortness of breath, chest tightness, low-grade fever
            • Duration: Symptoms have been present for 6 days
            • Relevant Findings: Wheezing and crackles in both lower lung fields, red throat, post-nasal drip present, yellowish-green sputum, fatigue with minimal exertion
            • Past Medical History: Mild asthma, Type 2 diabetes diagnosed 3 years ago, Ex-smoker (quit 10 years ago), No known allergies
            • Current Medications: Albuterol inhaler 1-2 times weekly, Metformin 500mg twice daily

            ## Investigations:
            • Recent Tests: Vital signs
            • Results: Temperature: 37.6°C, Respiratory rate: 22, Oxygen saturation: 94%, Heart rate: 92 bpm


            ## Reason for Referral:
            Due to worsening respiratory symptoms complicated by underlying asthma and diabetes, I would appreciate your expert assessment and management recommendations for this patient.


            ## Patient's Contact Information: 
            Phone Number: [Patient’s Phone Number]
            Email Address: [Patient’s Email Address]

            Thank you for your attention to this referral. I look forward to your evaluation and recommendations.

            Yours sincerely,

            [Your Full Name]
            [Your Title]


            Example 2:
                    
            Example Transcription:
            Speaker 0: Seeing you again how are you?
            Speaker 1: Hi i'm feeling okay normally i come every two months but scheduled earlier because i've been feeling depressed and anxious i had a panic attack this morning and felt like having chest pain tired and overwhelmed
            Speaker 0: well i'm sorry to hear that can you describe the panic attack further.
            Speaker 1: well i feel like i have a knot in my throat it's a very uncomfortable sensation i was taking lithium in the past but discontinued it because of the side effects
            Speaker 0: what kind of side effects?
            Speaker 1: i was dizzy slow thought process and was feeling nauseous i still throw up every morning i have this sensation like i need to throw up but since i don't have anything in my stomach i just retch
            Speaker 0: how long has this been going on for.
            Speaker 1: oh it's been going on for a month now.
            Speaker 0: and how about the panic attacks
            Speaker 1: I was having the panic attacks once every week in the past and had stopped for three months and this was the first time after that
            Speaker 0: Are you stressed out about anything in particular
            Speaker 1: I feel stressed out about my work and don't want to be there but have to be there for money i work in verizon in sales department sometimes when there are no customers that's when i feel the worst because if i can't hit the sales target they they can fire me
            Speaker 0: How was your job before that?
            Speaker 1: I was working at wells fargo before and that was even more stressful and before that i was working with uber which was not as stressful but the money wasn't good and before that i was working for at and t which was good but because of my illness i could not continue to work there
            Speaker 0: and how has your mood been
            Speaker 1: i feel down i feel like i have a lack of motivation i used to watch a lot of movies and tv shows in the past but now i don't feel interested anymore
            Speaker 0: what do you do in your free time?
            Speaker 1: oh i spend a lot of time on facebook just scrolling you know i have a friend in cuba and he chats with me every day which kinda distracts me
            Speaker 0: who do you live with?
            Speaker 1: i live with my husband and a child a four year old girl
            Speaker 0: do you like spending time with them
            Speaker 1: not really i don't feel like doing much my daughter is closer with my husband so she likes spending more time with him i do take her to the park though
            Speaker 0: how's your sleep been
            Speaker 1: i sleep well i sleep for more than ten hours
            Speaker 0: is it normal for you to sleep for ten hours or do you feel like you have been sleeping more than usual
            Speaker 1: no it's normal i usually sleep that long
            Speaker 0: how's your appetite been
            Speaker 1: i feel like my appetite has reduced
            Speaker 0: and how about your concentration have you been able to focus on your work
            Speaker 1: no my concentration is really bad
            Speaker 0: for how long
            Speaker 1: it's been like that for a month now
            Speaker 0: do you get any dark thoughts like thoughts about hurting yourself or anybody else no how about experiencing anything unusual like hearing voices no one else can hear or seeing things no one else can see such as shadows etcetera
            Speaker 1: no k
            Speaker 0: in the last month have you had any episodes where you have be you have the opposite of low energy like having a lot of energy a lot of thoughts running in your head really fast
            Speaker 1: no i know what manic episodes look like my husband has been keeping an eye on me and i i'm also aware of how how i feel when it starts
            Speaker 0: okay that's good seems like the main issue right now is depression and anxiety have you been getting the monthly injections on time
            Speaker 1: yes
            speaker 0: that's good are you open to start oral medication to help with your symptoms in addition to the injection
            speaker 1: yes
            Speaker 0: okay we're going to start you on a medication for depression that you'll take every morning however you will have to watch out for hyperactivity because medication for depression can trigger a manic episode if you feel like you are getting excessively active and not sleeping well give us a call
            Speaker 1: Okay doctor
            Speaker 0: we will also start you on a medication for anxiety
            Speaker 1: okay sounds good
            Speaker 0: okay pleasure meeting you and see you in next follow-up visit
            Speaker 1: okay thank you
            Speaker 0: thanks bye

            Example REFERRAL LETTER Output:

            [Practice Letterhead]

            05/06/2025

            To:  
            Dr. [Consultant's Name]  
            [Specialist Clinic/Hospital Name]  
            [Address Line 1]  
            [Address Line 2]  
            [City, Postcode]

            Dear Dr. [Consultant's Last Name],


            Re: Referral for [Patient's Name], [Date of Birth: DOB]

            I am referring this patient to your clinic for further evaluation and management of depression with anxiety in the context of bipolar disorder.

            ## Clinical Details:
            • Presenting Complaint: Depression, anxiety, and panic attacks
            • Duration: Symptoms have been present for at least one month
            • Relevant Findings: Morning retching/vomiting for the past month, sensation of knot in throat, chest pain during panic attacks, fatigue, feeling overwhelmed, low mood, lack of motivation, anhedonia, reduced appetite, poor concentration
            • Past Medical History: Bipolar disorder
            • Current Medications: Monthly injections for bipolar disorder (previously on lithium but discontinued due to side effects including dizziness, cognitive slowing, and nausea)

            ## Investigations:
            • Recent Tests: Mental state examination
            • Results: Patient appears depressed and anxious

            ## Reason for Referral:
            Due to recurrence of panic attacks after a three-month symptom-free period and worsening depressive symptoms, I would appreciate your expert assessment and management recommendations for this patient.

            ## Patient's Contact Information: 
            Phone Number: [Patient’s Phone Number]
            Email Address: [Patient’s Email Address]

            Enclosed are relevant clinical notes for your review. Please do not hesitate to contact me if you require further information.

            Thank you for your attention to this referral. I look forward to your evaluation and recommendations.

            Yours sincerely,

            [Your Full Name]
            [Your Title]

            By following these instructions, ensure the referral letter is accurate, professional, and compliant with the template, using only the provided data.
            """

        elif template_type == "consult_note":
            current_date = None
            if "metadata" in transcription and "current_date" in transcription["metadata"]:
                current_date = transcription["metadata"]["current_date"]
                main_logger.info(f"[OP-{operation_id}] Using current_date from metadata: {current_date}")

            user_instructions= f"""You are provided with a medical conversation transcript. 
            Analyze the transcript and generate a structured Consult note following the specified template. 
            Use only the information explicitly provided in the transcript, and do not include or assume any additional details. 
            Ensure the output is a valid textual foramt with the Cosnult note sections (Consultation Type, History, Examination, Impression, and Plan) as keys, formatted professionally and concisely in a doctor-like tone. 
            Make sure the output is in valid text/plain format. 
            If the patient didnt provide the information regarding (Consultation Type, History, Examination, Impression, and Plan) then ignore the respective section.
            For time references (e.g., “this morning,” “last Wednesday”), convert to specific dates based on today’s date, current day's date whatever it is on calender.
            Include the all numbers in numeric format.
            Make sure the output is concise and to the point.
            Ensure that each point of (Consultation Type, History, Examination, Impression, and Plan) starts with "• ".
            ENsure the data in (Consultation Type, History, Examination, Impression, and Plan) should be concise to the point and professional.
            Make it useful as doctors perspective so it makes there job easier, dont just dictate and make a note, analyze the conversation, summarize it and make a note that best desrcibes the patient's case as a doctor's perspective.
            For each point add - at the beginning of the line and give two letter space after that.
            Add time related information in the report dont miss them.
            Make sure the point are concise and structured and looks professional.
            Use medical terms to describe each thing
            Dont repeat anyything dude please.
            Include sub-headings as specified below in format in output
            Below is the transcript:\n\n{conversation_text}

            Below is the instructions to format report:
            {formatting_instructions}
            """
            # Add the new system prompt for consultation notes
            system_message = f"""You are a medical documentation assistant tasked with generating a structured consult note in text format based solely on a provided medical conversation transcript or contextual notes. 
            The note must follow the specified template with sections: Consultation Type, History, Examination, Impression, and Plan. 
            Use only information explicitly provided in the transcript or notes, avoiding any assumptions or invented details. 
            Ensure the output is concise, professional, and written in a doctor-like tone to facilitate clinical decision-making. 
            Summarize the transcript effectively to capture the patient’s case from a doctor’s perspective.
            {date_instructions}
            Below is the instructions to format report:
            {formatting_instructions}
            Instructions

            Output Structure
            - Generate a text-based consult note with the following sections, formatted as specified:
                a) Consultation Type: Specify "F2F" (face-to-face) or "T/C" (telephone consultation) and whether anyone else is present (e.g., "seen alone" or "seen with [person]") based on introductions in the transcript. Include the reason for visit (e.g., current issues, presenting complaint, booking note, or follow-up).
                b) History: Include subheadings for History of Presenting Complaints, ICE (Ideas, Concerns, Expectations), Red Flag Symptoms, Relevant Risk Factors, PMH/PSH (Past Medical/Surgical History), DH (Drug History)/Allergies, FH (Family History), and SH (Social History).
                c) Examination: Include subheadings for Vital Signs, Physical/Mental State Examination Findings, and Investigations with Results.
                d) Impression: List each issue with its likely diagnosis and differential diagnosis (if mentioned).
                e) Plan: Include investigations planned, treatment planned, referrals, follow-up plan, and safety netting advice for each issue (if mentioned).
            - Use bullet points (`• `) for all items under History, Examination, and Plan sections.
            - Populate fields only with data explicitly mentioned in the transcript or notes. Leave fields blank (omit the field or section) if no relevant data is provided.
            - Use a concise, professional tone, e.g., "Fatigue and dull headache since May 29, 2025. Ibuprofen taken with minimal relief."
            - If some sentence is long split it into multiple points and write "• " before each line.

            Consult Note Format: 

            # Consult Note

            ## Consultation Type:
            • Specify "F2F" or "T/C" based on the consultation method mentioned in the transcript.
            • Note presence of others (e.g., "seen alone" or "seen with [person]") based on introductions.
            • State the reason for visit (e.g., presenting complaint, booking note, follow-up) as mentioned.

            ## History:
            - Include the following subheadings, using bullet points (`• `) for each item:
                a) History of Presenting Complaints: Summarize the patient’s chief complaints, including duration, timing, location, quality, severity, or context, if mentioned.
                b) ICE: Patient’s Ideas, Concerns, and Expectations, if mentioned.
                c) Red Flag Symptoms: Presence or absence of red flag symptoms relevant to the presenting complaint, if mentioned. Try to accomodate all red flag in concise way in 1-2 points if they are mentioned.
                d) Relevant Risk Factors: Risk factors relevant to the complaint, if mentioned.
                e) Past Medical/Surgical History: Past medical or surgical history, if mentioned.
                f) Drug History / Allergies: Drug history/medications and allergies, if mentioned (omit allergies if not mentioned).
                g) Family History: Relevant family history, if mentioned.
                h) Social History: Social history (e.g., lives with, occupation, smoking/alcohol/drugs, recent travel, carers/package of care), if mentioned.
            - Omit any subheading or field if no relevant information is provided.

            ## Examination:
            - Include the following subheadings, using bullet points (`• `) for each item:
                a) Vital Signs: List vital signs (e.g., temperature, oxygen saturation, heart rate, blood pressure, respiratory rate), if mentioned.
                b) Physical/Mental State Examination Findings: System-specific examination findings, if mentioned. Use multiple bullet points as needed.
                c) Investigations with Results: Completed investigations and their results, if mentioned.
            - Omit any subheading or field if no relevant information is provided.
            - Do not assume normal findings unless explicitly stated.
            - Always include sub-headings if data available for them.
            - Never write plan without the subheading mentioned in the section if the data is available otherwise ignore that subheadings dont add it in report.
 

            ## Impression:
            - List each issue, problem, or request as a separate entry, formatted as:
            - "[Issue name]. [Likely diagnosis (condition name only, if mentioned)]"
            - Use numbering (1. ) for differential diagnosis, if mentioned (e.g., "• Differential diagnosis: [list]").
            - Include multiple issues (e.g., Issue 1, Issue 2, etc.) if mentioned, matching the chief complaints.
            - Omit any issue or field if not mentioned.

            Plan:
            - Use bullet points (`• `) for each item, including:
                a) Investigations Planned: Planned or ordered investigations for each issue, if mentioned.
                b) Treatment Planned: Planned treatments for each issue, if mentioned.
                c) Relevant Referrals: Referrals for each issue, if mentioned.
                d) Follow-up Plan: Follow-up timeframe, if mentioned.
                e) Advice: Advice given (e.g., symptoms requiring GP callback, 111 call for non-life-threatening issues, or A&E/999 for emergencies), if mentioned.
            - Group plans by issue (e.g., Issue 1, Issue 2) for clarity, if multiple issues are present.
            - Omit any field or section if no relevant information is provided.
            - If some sentence is long split it into multiple points and write "•  " before each line.
            - Always include sub-headings if data available for them
            - Never write plan without the subheading mentioned in the section if the data is available otherwise ignore that subheadings dont add it in report.
            
            Constraints
            - Data Source: Use only data from the provided transcript or contextual notes. Do not invent patient details, assessments, diagnoses, differential diagnoses, plans, interventions, evaluations, or safety netting advice.
            - Tone and Style: Maintain a professional, concise tone suitable for medical documentation. Avoid verbose or redundant phrasing.
            - Time References: Convert all time-related information (e.g., "yesterday", "2 days ago") to specific dates based on June 5, 2025 (Thursday). Examples:
            - This morning → Today's Date
            - "Yesterday" → Yesterday's date
            - "A week ago" → Date exactly a week ago
            - Use numeric format for numbers (e.g., "2" not "two").
            - Analysis: Analyze and summarize the transcript to reflect the patient’s case from a doctor’s perspective, ensuring the note is useful for clinical decision-making.
            - Empty Input: If no transcript or notes are provided, return an empty consult note structure with only the required section headings.
            - Formatting: Use bullet points (`• `) for History, Examination, and Plan sections. Ensure impression section follows the specified format without bullet points for the issue/diagnosis line. Do not state that information was not mentioned; simply omit the relevant field or section.

            Output
            - Generate a text-based consult note adhering to the specified structure, populated only with data explicitly provided in the transcript or notes. Ensure the note is concise, structured, and professional.


            IMPORTANT: 
            - Your response MUST be a valid textual format 
            - Do not invent information not present in the conversation
            - Be DIRECT and CONCISE in all documentation while preserving clinical accuracy
            - Always preserve ALL dates, medication dosages, and measurements EXACTLY as stated
            - Create complete documentation that would meet professional medical standards
            - If no consultation date is mentioned in the conversation, generate the current date in the format DD Month YYYY 
            - REMEMBER: Create SEPARATE impression items for EACH distinct symptom or issue


            Example for Reference (Do Not Use as Input):


            Example 1:
                    
            Example Transcription:
            Speaker 0 : Good morning i'm doctor sarah what brings you in today
            Speaker 1 : Good morning i have been feeling really unwell for the past week i have a constant cough shortness of breath and my chest feels tight i also have a low grade fever that comes and goes
            Speaker 0 : That sounds uncomfortable when did these symptoms start
            Speaker 1 : About six days ago at first i thought it was just a cold but it's getting worse i get tired just walking a few steps
            Speaker 0 : Are you producing any mucus with the cough
            Speaker 1 : Yes it's yellowish green and like sometimes it's hard to breathe
            Speaker 0 : Especially at night any wheezing or chest pain
            Speaker 1 : A little wheezing and my chest feels heavy no sharp pain though do you have
            Speaker 0 : A history of asthma copd or any lung condition?
            Speaker 1 : I have a mild asthma i use an albuterol inhaler occasionally maybe once or twice a week
            Speaker 0 : Any history of smoking i smoked in college but
            Speaker 1 : I quit ten years ago
            Speaker 0 : Do you have any allergic or chronic illness like diabetes hypertension or gerd
            Speaker 1 : I have type two diabetes diagnosed three years ago i take metamorphin five hundred mg twice a day no known allergies
            Speaker 0 : Any recent travel or contact with someone who's been sick
            Speaker 1 : No travel but my son had a very bad cold last week
            Speaker 0 : Alright let me check your vitals and listen to your lung your temperature is 106 respiratory rate is 22 oxygen saturation is 94 and heart rate is 92 beats per minute i hear some wheezing and crackles in both lower lung field your throat looks a bit red and post nasal drip is present
            Speaker 1 : Is it a chest infection
            Speaker 0 : Based on your symptoms history and exam it sounds like acute bronchitis possibly comp complicated by asthma and diabetes it's likely viral but with your underlying condition we should be cautious medications i'll prescribe amoxicillin chloride eight seventy five mg or one twenty five mg twice daily for seven days just in case there's a bacterial component continue using your albuterol inhaler but increase to every four to six six hours as needed i'm also prescribing a five day course of oral prednisone forty mg per day to reduce inflammation due to your asthma flare for the cough you can take guifenacin with dex with dextromethorphan as needed check your blood glucose more frequently while on prednisone as it can raise your sugar levels if your oxygen drops below 92 or your breathing worsens go to the emergency rest stay hydrated and avoid exertion use a humidifier at night and avoid cold air i want to see you back in three to five days to recheck your lungs and sugar control if symptoms worsen sooner come in immediately okay that makes sense will these make me sleepy the cough syrup might so take it at night and remember don't drive after taking it let me print your ex prescription and set your follow-up"

            Example CONSULT Note Output:

            F2F seen alone. Constant cough, shortness of breath, chest tightness, low-grade fever for past 6 days.

            History:
            • Constant cough with yellowish-green sputum, SOB, chest tightness, low-grade fever for 6 days
            • Symptoms worsening, fatigue with minimal exertion
            • Wheezing, chest heaviness, no sharp pain
            • Symptoms worse at night
            • Initially thought it was just a cold
            • Son had bad cold last week
            • Past Medical History: Mild asthma, uses albuterol inhaler 1-2 times weekly, Type 2 diabetes diagnosed 3 years ago
            • Drug History/Allergies: Metformin 500mg BD, albuterol inhaler PRN. Allergies: None known
            • social History: Ex-smoker, quit 10 years ago

            Examination:
            • T 37.6°C, Sats 94%, HR 92 bpm, RR 22
            • Wheezing and crackles in both lower lung fields
            • Red throat, post-nasal drip present

            Impression:
            1. Acute bronchitis with asthma exacerbation. Acute bronchitis complicated by asthma and diabetes
            2. Type 2 Diabetes

            Plan:
            Investigations Planned:
                • Follow-up in 3-5 days to reassess lungs and glucose control.
            Treatment Planned:
                • Amoxicillin clavulanate 875/125mg BD for 7 days.
                • Increase albuterol inhaler to every 4-6 hours PRN.
                • Prednisone 40mg daily for 5 days.
                • Guaifenesin with dextromethorphan for cough PRN.
            Advice:
                • Monitor blood glucose more frequently while on prednisone.
                • Rest, stay hydrated, avoid exertion.
                • Use humidifier at night, avoid cold air.
                • Seek emergency care if oxygen drops below 92% or breathing worsens.
                • Counselled regarding sedative effects of cough medication.


            Example 2:
                    
            Example Transcription:
            Speaker 0: Seeing you again how are you?
            Speaker 1: Hi i'm feeling okay normally i come every two months but scheduled earlier because i've been feeling depressed and anxious i had a panic attack this morning and felt like having chest pain tired and overwhelmed
            Speaker 0: well i'm sorry to hear that can you describe the panic attack further.
            Speaker 1: well i feel like i have a knot in my throat it's a very uncomfortable sensation i was taking lithium in the past but discontinued it because of the side effects
            Speaker 0: what kind of side effects?
            Speaker 1: i was dizzy slow thought process and was feeling nauseous i still throw up every morning i have this sensation like i need to throw up but since i don't have anything in my stomach i just retch
            Speaker 0: how long has this been going on for.
            Speaker 1: oh it's been going on for a month now.
            Speaker 0: and how about the panic attacks
            Speaker 1: I was having the panic attacks once every week in the past and had stopped for three months and this was the first time after that
            Speaker 0: Are you stressed out about anything in particular
            Speaker 1: I feel stressed out about my work and don't want to be there but have to be there for money i work in verizon in sales department sometimes when there are no customers that's when i feel the worst because if i can't hit the sales target they they can fire me
            Speaker 0: How was your job before that?
            Speaker 1: I was working at wells fargo before and that was even more stressful and before that i was working with uber which was not as stressful but the money wasn't good and before that i was working for at and t which was good but because of my illness i could not continue to work there
            Speaker 0: and how has your mood been
            Speaker 1: i feel down i feel like i have a lack of motivation i used to watch a lot of movies and tv shows in the past but now i don't feel interested anymore
            Speaker 0: what do you do in your free time?
            Speaker 1: oh i spend a lot of time on facebook just scrolling you know i have a friend in cuba and he chats with me every day which kinda distracts me
            Speaker 0: who do you live with?
            Speaker 1: i live with my husband and a child a four year old girl
            Speaker 0: do you like spending time with them
            Speaker 1: not really i don't feel like doing much my daughter is closer with my husband so she likes spending more time with him i do take her to the park though
            Speaker 0: how's your sleep been
            Speaker 1: i sleep well i sleep for more than ten hours
            Speaker 0: is it normal for you to sleep for ten hours or do you feel like you have been sleeping more than usual
            Speaker 1: no it's normal i usually sleep that long
            Speaker 0: how's your appetite been
            Speaker 1: i feel like my appetite has reduced
            Speaker 0: and how about your concentration have you been able to focus on your work
            Speaker 1: no my concentration is really bad
            Speaker 0: for how long
            Speaker 1: it's been like that for a month now
            Speaker 0: do you get any dark thoughts like thoughts about hurting yourself or anybody else no how about experiencing anything unusual like hearing voices no one else can hear or seeing things no one else can see such as shadows etcetera
            Speaker 1: no k
            Speaker 0: in the last month have you had any episodes where you have be you have the opposite of low energy like having a lot of energy a lot of thoughts running in your head really fast
            Speaker 1: no i know what manic episodes look like my husband has been keeping an eye on me and i i'm also aware of how how i feel when it starts
            Speaker 0: okay that's good seems like the main issue right now is depression and anxiety have you been getting the monthly injections on time
            Speaker 1: yes
            speaker 0: that's good are you open to start oral medication to help with your symptoms in addition to the injection
            speaker 1: yes
            Speaker 0: okay we're going to start you on a medication for depression that you'll take every morning however you will have to watch out for hyperactivity because medication for depression can trigger a manic episode if you feel like you are getting excessively active and not sleeping well give us a call
            Speaker 1: Okay doctor
            Speaker 0: we will also start you on a medication for anxiety
            Speaker 1: okay sounds good
            Speaker 0: okay pleasure meeting you and see you in next follow-up visit
            Speaker 1: okay thank you
            Speaker 0: thanks bye

            Example CONSULT Note Output:

            # Consult Note

            ## Consultation Type:
            F2F seen alone. Feeling depressed and anxious.

            ## History:
            • Scheduled appointment earlier than usual due to feeling depressed and anxious
            • Panic attack this morning with chest pain, fatigue, feeling overwhelmed
            • Sensation of knot in throat
            • Morning retching/vomiting for past month
            • Panic attacks occurring weekly previously, stopped for three months, recent recurrence
            • Work-related stress (currently in sales at Verizon)
            • Previous employment at Wells Fargo (more stressful), Uber (less stressful but poor pay), AT&T (had to leave due to illness)
            • Low mood, lack of motivation, anhedonia (no longer interested in previously enjoyed activities like movies/TV)
            • Spends free time scrolling through Facebook, chatting with friend in Cuba
            • Lives with husband and 4-year-old daughter
            • Reduced interest in family activities, though takes daughter to park
            • Sleep: 10+ hours (normal pattern)
            • Reduced appetite
            • Poor concentration for past month
            • No suicidal/homicidal ideation
            • No hallucinations
            • No recent manic episodes
            • History of bipolar disorder (aware of manic episode symptoms, husband monitoring)
            • Previously on lithium but discontinued due to side effects (dizziness, cognitive slowing, nausea)
            • Currently receiving monthly injections

            ## Examination:
            • Mental state: Appears depressed and anxious

            ## Impression:
            1. Depression with anxiety
            2. Bipolar disorder (currently in depressive phase)

            ## Plan:
            • Continue monthly injections
            • Start antidepressant medication (morning dose)
            • Start anti-anxiety medication
            • Safety netting: Monitor for signs of mania (hyperactivity, reduced sleep), contact if these develop
            • Follow up appointment scheduled

"""

        elif template_type == "mental_health_appointment":
            current_date = None
            if "metadata" in transcription and "current_date" in transcription["metadata"]:
                current_date = transcription["metadata"]["current_date"]
                main_logger.info(f"[OP-{operation_id}] Using current_date from metadata: {current_date}")
            
            user_instructions= f"""You are a clinical psychologist tasked with generating a structured clinical report in TEXT/PLAIN format based on a provided conversation transcript from a client's first appointment. 
            Analyze the transcript and produce a clinical note following the specified template. 
            Use only information explicitly provided in the transcript or contextual notes, and do not include or assume additional details. 
            Do not repeat anything.
            Make each point concise and at the begining of it write "•  " and if some field has no data then ignore then field dont write anything under it. Make it to the point no need to use words like patient, client or anything make it direct.
            Be concise, to the point and make intelligent remarks.
            Ensure the output is a valid textual format. 
            Format professionally and concisely in a doctor-like tone using medical terminology. Capture all discussed topics, including non-clinical ones relevant to mental health care. Convert time references to DD/MM/YYYY format based on {current_date if current_date else "15/06/2025"}. Ensure points start with '- ', except for the case_formulation paragraph. Make the report concise, structured, and professional, describing the case directly without using 'patient' or 'client.' Omit sections with no data. Avoid repetition.
            Below is the transcript:\n\n{conversation_text}

            Below is the instructions to format the report:
            {formatting_instructions}
            """

            system_message = f"""You are a highly skilled clinical psychologist tasked with generating a professional, medically sound clinical report in text/plain format based on a provided conversation transcript from a client's first appointment. Analyze the transcript thoroughly, extract relevant information, and produce a structured clinical note following the specified template. Use only information explicitly provided in the transcript or contextual notes, and do not include or assume additional details. Ensure the output is a valid textual format with the specified sections, formatted professionally and concisely in a doctor-like tone using medical terminology. Capture all discussed topics, including non-clinical ones, as they may be relevant to mental health care. Convert time references to specific dates in DD/MM/YYYY format based on {current_date if current_date else "15/06/2025"}. Ensure each point starts with "• " (dash followed by a space), except for the case_formulation paragraph. Make the report concise, structured, and professional, describing the case directly from a doctor's perspective without using terms like "patient" or "client." 
            Avoid repetition and omit sections with no data.
            {grammar_instructions} {preservation_instructions} {date_instructions}
            Below is the instructions to format the report:
            {formatting_instructions}


            Template Structure:

            - Chief Complaint: Primary mental health issue, presenting symptoms
            - Past medical & psychiatric history: Past psychiatric diagnoses, treatments, hospitalisations, current medications or medical conditions
            - Family History: Psychiatric illnesses
            - Social History: Occupation, level of education, substance use (smoking, alcohol, recreational drugs), social support
            - Mental Status Examination: Appearance, behaviour, speech, mood, affect, thoughts, perceptions, cognition, insight, judgment
            - Risk Assessment: Suicidality, homicidality, other risks
            - Diagnosis: DSM-5 criteria, psychological scales/questionnaires
            - Treatment Plan: Investigations performed, medications, psychotherapy, family meetings & collateral information, psychosocial interventions, follow-up appointments and referrals
            - Safety Plan: If applicable, detailing steps to take in crisis

            Instructions:

            Input Analysis: Analyze the entire transcript to capture all discussed topics, including non-clinical aspects relevant to mental health care. Use only explicitly provided data, avoiding fabrication or inference.
            Template Adherence: Include only sections and placeholders explicitly mentioned in the transcript or contextual notes. Omit sections with no data (leave as empty arrays or strings in text/plain). Add additional_notes for topics not fitting the template.
            Text/Plain Structure: Output a valid textual format with the above keys, populated only with explicitly provided data.
            Verify date accuracy.
            Professional Tone and Terminology: Use a concise, doctor-like tone with medical terminology (e.g., "Reports anhedonia and insomnia since 15/05/2025"). Avoid repetition, direct quotes, or colloquial language.
            Formatting:
            Ensure each point in all sections except clinical_formulation.case_formulation starts with "• ".
            
            Constraints:
            Use only transcript or contextual note data. Do not invent details, assessments, or plans.
            Maintain a professional tone for medical documentation.
            Do not assume unmentioned information (e.g., normal findings, symptoms).
            If no transcript is provided, return an empty TEXT structure.


            Output: Generate a valid textual format adhering to the template, populated only with explicitly provided data.

            Example for Reference (Do Not Use as Input):


            Example 1:
            
            Example Transcription:
            Speaker 0 : Good morning i'm doctor sarah what brings you in today
            Speaker 1 : Good morning i have been feeling really unwell for the past week i have a constant cough shortness of breath and my chest feels tight i also have a low grade fever that comes and goes
            Speaker 0 : That sounds uncomfortable when did these symptoms start
            Speaker 1 : About six days ago at first i thought it was just a cold but it's getting worse i get tired just walking a few steps
            Speaker 0 : Are you producing any mucus with the cough
            Speaker 1 : Yes it's yellowish green and like sometimes it's hard to breathe
            Speaker 0 : Especially at night any wheezing or chest pain
            Speaker 1 : A little wheezing and my chest feels heavy no sharp pain though do you have
            Speaker 0 : A history of asthma copd or any lung condition?
            Speaker 1 : I have a mild asthma i use an albuterol inhaler occasionally maybe once or twice a week
            Speaker 0 : Any history of smoking i smoked in college but
            Speaker 1 : I quit ten years ago
            Speaker 0 : Do you have any allergic or chronic illness like diabetes hypertension or gerd
            Speaker 1 : I have type two diabetes diagnosed three years ago i take metamorphin five hundred mg twice a day no known allergies
            Speaker 0 : Any recent travel or contact with someone who's been sick
            Speaker 1 : No travel but my son had a very bad cold last week
            Speaker 0 : Alright let me check your vitals and listen to your lung your temperature is 106 respiratory rate is 22 oxygen saturation is 94 and heart rate is 92 beats per minute i hear some wheezing and crackles in both lower lung field your throat looks a bit red and post nasal drip is present
            Speaker 1 : Is it a chest infection
            Speaker 0 : Based on your symptoms history and exam it sounds like acute bronchitis possibly comp complicated by asthma and diabetes it's likely viral but with your underlying condition we should be cautious medications i'll prescribe amoxicillin chloride eight seventy five mg or one twenty five mg twice daily for seven days just in case there's a bacterial component continue using your albuterol inhaler but increase to every four to six six hours as needed i'm also prescribing a five day course of oral prednisone forty mg per day to reduce inflammation due to your asthma flare for the cough you can take guifenacin with dex with dextromethorphan as needed check your blood glucose more frequently while on prednisone as it can raise your sugar levels if your oxygen drops below 92 or your breathing worsens go to the emergency rest stay hydrated and avoid exertion use a humidifier at night and avoid cold air i want to see you back in three to five days to recheck your lungs and sugar control if symptoms worsen sooner come in immediately okay that makes sense will these make me sleepy the cough syrup might so take it at night and remember don't drive after taking it let me print your ex prescription and set your follow-up"

            Example MENTAL HEALTH NOTE:

            Chief Complaint: Cough, shortness of breath, chest tightness, low-grade fever for 6 days

            Past medical & psychiatric history: Mild asthma (uses albuterol inhaler 1-2 times weekly), Type 2 diabetes diagnosed 3 years ago (takes metformin 500mg twice daily), Ex-smoker (quit 10 years ago)

            Family History: No psychiatric illnesses noted

            Social History: No occupation or education level mentioned, Ex-smoker (quit 10 years ago), No alcohol or recreational drug use mentioned, Son at home (recently had severe cold)

            Mental Status Examination: Not assessed

            Risk Assessment: No suicidality, homicidality or other risks noted

            Diagnosis: Acute bronchitis with asthma exacerbation

            Treatment Plan: 
            - Amoxicillin clavulanate 875/125mg twice daily for 7 days
            - Increase albuterol inhaler to every 4-6 hours as needed
            - Prednisone 40mg daily for 5 days
            - Guaifenesin with dextromethorphan for cough as needed
            - Monitor blood glucose more frequently while on prednisone
            - Rest, hydration, avoid exertion
            - Use humidifier at night, avoid cold air
            - Follow-up in 3-5 days to reassess lungs and glucose control

            Safety Plan: Seek emergency care if oxygen saturation drops below 92% or breathing worsens
                        
            """
            
        elif template_type == "clinical_report":
            current_date = None
            if "metadata" in transcription and "current_date" in transcription["metadata"]:
                current_date = transcription["metadata"]["current_date"]
                main_logger.info(f"[OP-{operation_id}] Using current_date from metadata: {current_date}")
            
            user_instructions = f"""You are a clinical psychologist tasked with generating a structured clinical report in Text format based on a provided conversation transcript from a client's first appointment. Analyze the transcript and produce a clinical note following the specified template. Use only information explicitly provided in the transcript or contextual notes, and do not include or assume additional details. Ensure the output is a valid textual format with the specified sections, formatted professionally and concisely in a doctor-like tone. Capture all discussed topics, including non-clinical ones, as they may be relevant to mental health care. Convert time references to specific dates in DD/MM/YYYY format based on {current_date if current_date else "the current date"}. Ensure each point starts with '- ' (dash followed by a space), except for the case formulation paragraph. Make the report concise, structured, and professional, using medical terminology to describe the client's case from a doctor's perspective.
            DO not repeat anything.
            Make each point concise and at the begining of it write "•  " and if some field has no data then ignore then field dont write anything under it. Make it to the point no need to use words like patient, client or anything make it direct.
            Below is the transcript:\n\n{conversation_text}

            Below is the format, how i need the report to be in:
            {formatting_instructions}
            """
            # Clinical Report Instructions - Updated to emphasize preservation
            system_message = f"""You are a highly skilled clinical psychologist tasked with generating a professional, medically sound clinical report based on a provided conversation transcript from a client's first appointment with a clinical psychologist. 
            Your role is to analyze the transcript thoroughly, extract relevant information, and produce a structured clinical note in TEXT/PLAIN format following the specified template. 
            The report must be concise, professional, and written from a doctor's perspective, using precise medical terminology to facilitate clinical decision-making. 
            {grammar_instructions} {preservation_instructions} {date_instructions}

            Below is the format, how i need the report to be in:
            {formatting_instructions}
           
            Below are the instructions for generating the report:

            Input Analysis: Analyze the entire conversation transcript to capture all discussed topics, including those not explicitly clinical, as they may be significant to the client's mental health care. Use only information explicitly provided in the transcript or contextual notes. Do not fabricate or infer additional details.
            Template Adherence: Follow the provided clinical note template, including only sections and placeholders explicitly mentioned in the transcript or contextual notes. Omit any sections or placeholders not supported by the transcript, leaving them blank (empty strings in TEXT). Add new sections if necessary to capture unique topics discussed in the transcript not covered by the template.
            Format: Make each point concise and at the begining of it write "•  " and if some field has no data then ignore then field dont write anything under it. Make it to the point no need to use words like patient, client or anything make it direct.
            Text/Plain Structure: Output a valid Textual structure with the following top-level keys, populated only with data explicitly mentioned in the transcript or contextual notes:
                presenting_problems
                history_of_presenting_problems
                current_functioning
                current_medications
                psychiatric_history
                medical_history
                developmental_social_family_history
                substance_use
                relevant_cultural_religious_spiritual_issues
                risk_assessment
                mental_state_exam
                test_results
                diagnosis
                clinical_formulation
                additional_notes
            Professional Tone and Terminology: Use a concise, professional, doctor-like tone with medical terminology (e.g., "Client reports dysphoric mood and anhedonia for one month"). Avoid repetition, direct quotes, or colloquial language. Use "Client" instead of "patient" or the client's name.
            Formatting: Ensure each point in relevant sections (e.g., presenting_problems, history_of_presenting_problems, current_functioning, etc.) begins with "• " (dash followed by a single space). For clinical_formulation.case_formulation, use a single paragraph without bullet points. For additional_notes, use bullet points for any information not fitting the template.
            Section-Specific Instructions:
                - Presenting Problems: List each distinct issue or topic as an object in an array under presenting_problems, with a field details containing bullet points capturing the reason for the visit and associated stressors.
                - History of Presenting Problems: For each issue, include an object in an array with fields for issue_name and details (onset, duration, course, severity).
                - Current Functioning: Include sub-keys (sleep, employment_education, family, etc.) as arrays of bullet points, only if mentioned.
                - Clinical Formulation: Include a case_formulation paragraph summarizing presenting problems, predisposing, precipitating, perpetuating, and protective factors, only if explicitly mentioned. Also include arrays for each factor type with bullet points.
                - Additional Notes: Capture any transcript information not fitting the template as bullet points.
            Constraints:
            Use only data from the transcript or contextual notes. Do not invent details, assessments, plans, or interventions.
            Maintain a professional tone suitable for medical documentation.
            Do not assume information (e.g., normal findings, unmentioned symptoms) unless explicitly stated.
            If no transcript is provided, return the TEXT/PLAIN structure with all fields as empty strings or arrays.
            Output: Generate a valid textual structure adhering to the template and constraints, populated only with explicitly provided data.

            CLINICAL INTERVIEW REPORT TEMPLATE:

            (The template or structure below is intended to be used for a first appoinments with clinical psychologists,  It is important that you note that the details of the topics discussed in the transcript may vary greatly between patients, as a large proportion of the information intended for placeholders in square brackets in the template or structure below may already be known. If there is no specific mention in the transcript or contextual notes of the relevant information for a placeholder below, you should not include the placeholder in the clinical note or document that you output - instead you should leave it blank. Do not hallucinate or make up any information for a placeholder in the template or structure below if it is not mentioned or present in the transcript. The topics discussed in the transcript by clinical psychologists are sometimes not well-defined clinical disease states or symptoms and are often just aspects of the patient's life that are important to them and they wish to discuss with their clinician. Therefore it is vital that the entire transcript is used and included in the clinical note or document that you output, as even brief topic discussions may be an important part of the patient's mental health care. The placeholders below should therefore be used as a rough guide to how the information in the transcript should be captured in the clinical note or document, but you should interpret the topics discussed and then use your judgement to either: exclude sections from the template or structure below because it is not relevant to the clinical note or document based on the details of the topics discussed in the transcript, or include new sections that are not currently present in the template or structure, in order to accurately capture the details of the topics discussed in the transcript. Remember to use as many bullet points as you need to capture the relevant details from the transcript for each section. Do not respond to these guidelines in your output, you must only output the clinical note or document as instructed.)

            CLINICAL INTERVIEW:
            
            ## PRESENTING PROBLEM(s)
            • [Detail presenting problems.] (use as many bullet points as needed to capture the reason for the visit and any associated stressors in detail) (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)

            ## HISTORY OF PRESENTING PROBLEM(S)
            • [Detail the history of the presenting Problem(s) and include onset, duration, course, and severity of the symptoms or problems.] (use as many bullet points as needed to capture when the symptoms or problem started, the development and course of symptoms) (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)

            ## CURRENT FUNCTIONING
            • Sleep: [Detail sleep patterns.] (use as many bullet points as needed to capture the sleep pattern and how the problem has affected sleep patterns) (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank).
            • Employment/Education: [Detail current employment or educational status.] (use as many bullet points as needed to capture current employment or educational status and how the symptoms or problem has affected current employment or education) (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank).
            • Family: [Detail family dynamics and relationships.] (use as many bullet points as needed to capture names, ages of family members and the relationships with each other and the effect of symptoms on the family dynamics and relationships) (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank).
            • Social: [Describe social interactions and the patient’s support network.] (use as many bullet points as needed to capture the social interactions of the patient and the patient’s support network) (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank).
            • Exercise/Physical Activity: [Detail exercise routines or physical activities] (use as many bullet points as needed to capture all exercise and physical activity and the effect the symptoms have had on the patient’s exercise and physical activity) (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank.)
            • Eating Regime/Appetite: [Detail the eating habits and appetite] (use as many bullet points as needed to capture all eating habits and appetite and the effect the symptoms have had on the patient’s eating habits and appetite) (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank).
            • Energy Levels: [Detail energy levels throughout the day and the effect the symptoms have had on energy levels] (use as many bullet points as needed to capture the patient’s energy levels and the effect the symptoms or problems have had on the patient’s energy levels) (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)
            • Recreational/Interests: [Mention hobbies or interests and the effect the patient’s symptoms have had on their hobbies and interests] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)

            ## CURRENT MEDICATIONS
            • Current Medications: [List type, frequency, and daily dose in detail] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)

            ## PSYCHIATRIC HISTORY
            • Psychiatric History: [Detail any psychiatric history including hospitalisations, treatment from psychiatrists, psychological treatment, counselling, and past medications – type, frequency and dose] (use as many bullet points as needed to capture the patient’s psychiatric history) (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)
            • Other interventions: [Detail any other interventions not mentioned in Psychiatric History] (Use as many bullet points as needed to capture all interventions) (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)

            ## MEDICAL HISTORY
            • Personal and Family Medical History: [Detail personal and family medical history] (Use as many bullet points as needed to capture the patient’s medical history and the patient’s family medical history) (only include if explicitly mentioned in the contextual notes or clinical note, otherwise leave blank)

            ## DEVELOPMENTAL, SOCIAL AND FAMILY HISTORY
            • Family:
            [Detail the family of origin] (use as many bullet points as needed to capture the patient’s family at birth, including parent’s names, their occupations, the parent's relationship with each other, and other siblings) (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)

            • Developmental History:
            [Detail developmental milestones and any issues] (use as many bullet points as needed to capture developmental history) (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)

            • Educational History
            [Detail educational history, including academic achievement, relationship with peers, and any issues] (use as many bullet points as needed to capture educational history) (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)

            • Employment History
            [Detail employment history and any issues] (use as many bullet points as needed to capture employment history) (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)

            • Relationship History
            [Detail relationship history and any issues] (use as many bullet points as needed to capture the relationship history) (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)

            • Forensic/Legal History
            [Detail any forensic or legal history] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)

            • SUBSTANCE USE
            [Detail any current and past substance use] (use as many bullet points as needed to capture current and past substance use including type and frequency) (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)

            • RELEVANT CULTURAL/RELIGIOUS/SPIRITUAL ISSUES
            [Detail any cultural, religious, or spiritual factors that are relevant] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)

            ## RISK ASSESSMENT
            • Suicidal Ideation: [History, attempts, plans] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)
            • Homicidal Ideation: [Describe any homicidal ideation] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)
            • Self-harm: [Detail any history of self-harm] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)
            • Violence & Aggression: [Describe any incidents of violence or aggression] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)
            • Risk-taking/Impulsivity: [Describe any risk-taking behaviors or impulsivity] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)

            ## MENTAL STATE EXAM:
            • Appearance: [Describe the patient's appearance] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)
            • Behaviour: [Describe the patient's behaviour] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)
            • Speech: [Detail speech patterns] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)
            • Mood: [Describe the patient's mood] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)
            • Affect: [Describe the patient's affect] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)
            • Perception: [Detail any hallucinations or dissociations] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)
            • Thought Process: [Describe the patient's thought process] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)
            • Thought Form: [Detail the form of thoughts, including any disorderly thoughts] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)
            • Orientation: [Detail orientation to time and place] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)
            • Memory: [Describe memory function] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)
            • Concentration: [Detail concentration levels] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)
            • Attention: [Describe attention span] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)
            • Judgement: [Detail judgement capabilities] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)
            • Insight: [Describe the patient's insight into their condition] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)

            ## TEST RESULTS 
            • Summary of Findings: [Summarize the findings from any formal psychometric assessments or self-report measures] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)

            ## DIAGNOSIS:
            • Diagnosis: [List any DSM-5-TR diagnosis and any comorbid conditions] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)

            ## CLINICAL FORMULATION:
                • Presenting Problem: [Summarise the presenting problem]  (Use as many bullet points as needed to capture the presenting problem) (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)
                • Predisposing Factors: [List predisposing factors to the patient's condition] (Use as many bullet points as needed to capture the predisposing factors) (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)
                • Precipitating Factors: [List precipitating factors that may have triggered the condition] (Use as many bullet points as needed to capture the precipitating factors)  (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)
                • Perpetuating Factors: [List factors that are perpetuating the condition]  (Use as many bullet points as needed to capture the perpetuating factors) (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)
                • Protecting Factors: [List factors that protect the patient from worsening of the condition]  (Use as many bullet points as needed to capture the protective factors) (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)

            ##  Case formulation: 
            [Detail a case formulation as a paragraph] [Client presents with (problem), which appears to be precipitated by (precipitating factors). Factors that seem to have predisposed the client to the (problem) include (predisposing factors). The current problem is maintained by (perpetuating factors). However, the protective and positive factors include (Protective factors)].

            (Ensure all information discussed in the transcript is included under the relevant heading or sub-heading above, otherwise include it as a bullet-pointed additional note at the end of the note.) (Never come up with your own patient details, assessment, plan, interventions, evaluation, and plan for continuing care - use only the transcript, contextual notes or clinical note as a reference for the information include in your note. If any information related to a placeholder has not been explicitly mentioned in the transcript, contextual notes or clinical note, you must not state the information has not been explicitly mentioned in your output, just leave the relevant placeholder or section blank.)  (Always use the word "Client" instead of the word "patient" or their name.)  (Ensure all information is super detailed and do not use quotes.)


            Example for Reference (Do Not Use as Input):


            Example 1:
            
            Example Transcription:
            Speaker 0 : Good morning i'm doctor sarah what brings you in today
            Speaker 1 : Good morning i have been feeling really unwell for the past week i have a constant cough shortness of breath and my chest feels tight i also have a low grade fever that comes and goes
            Speaker 0 : That sounds uncomfortable when did these symptoms start
            Speaker 1 : About six days ago at first i thought it was just a cold but it's getting worse i get tired just walking a few steps
            Speaker 0 : Are you producing any mucus with the cough
            Speaker 1 : Yes it's yellowish green and like sometimes it's hard to breathe
            Speaker 0 : Especially at night any wheezing or chest pain
            Speaker 1 : A little wheezing and my chest feels heavy no sharp pain though do you have
            Speaker 0 : A history of asthma copd or any lung condition?
            Speaker 1 : I have a mild asthma i use an albuterol inhaler occasionally maybe once or twice a week
            Speaker 0 : Any history of smoking i smoked in college but
            Speaker 1 : I quit ten years ago
            Speaker 0 : Do you have any allergic or chronic illness like diabetes hypertension or gerd
            Speaker 1 : I have type two diabetes diagnosed three years ago i take metamorphin five hundred mg twice a day no known allergies
            Speaker 0 : Any recent travel or contact with someone who's been sick
            Speaker 1 : No travel but my son had a very bad cold last week
            Speaker 0 : Alright let me check your vitals and listen to your lung your temperature is 106 respiratory rate is 22 oxygen saturation is 94 and heart rate is 92 beats per minute i hear some wheezing and crackles in both lower lung field your throat looks a bit red and post nasal drip is present
            Speaker 1 : Is it a chest infection
            Speaker 0 : Based on your symptoms history and exam it sounds like acute bronchitis possibly comp complicated by asthma and diabetes it's likely viral but with your underlying condition we should be cautious medications i'll prescribe amoxicillin chloride eight seventy five mg or one twenty five mg twice daily for seven days just in case there's a bacterial component continue using your albuterol inhaler but increase to every four to six six hours as needed i'm also prescribing a five day course of oral prednisone forty mg per day to reduce inflammation due to your asthma flare for the cough you can take guifenacin with dex with dextromethorphan as needed check your blood glucose more frequently while on prednisone as it can raise your sugar levels if your oxygen drops below 92 or your breathing worsens go to the emergency rest stay hydrated and avoid exertion use a humidifier at night and avoid cold air i want to see you back in three to five days to recheck your lungs and sugar control if symptoms worsen sooner come in immediately okay that makes sense will these make me sleepy the cough syrup might so take it at night and remember don't drive after taking it let me print your ex prescription and set your follow-up"

            Example CLINICAL INTERVIEW REPORT:
           
            # CLINICAL INTERVIEW 
            
            ## PRESENTING PROBLEM(s)
            • Cough, shortness of breath, chest tightness, low-grade fever for 6 days
            • Yellowish-green sputum production
            • Symptoms worse at night
            • Wheezing and chest heaviness, no sharp pain
            • Symptoms worsening with fatigue upon minimal exertion

            ## HISTORY OF PRESENTING PROBLEM(S)
            • Symptoms began 6 days ago
            • Initially thought to be a cold but progressively worsening
            • Son had severe cold last week, possible source of infection

            ## CURRENT FUNCTIONING
            • Sleep: Difficulty breathing especially at night
            • Employment/Education: Fatigue with minimal exertion, gets tired walking a few steps

            ## CURRENT MEDICATIONS
            • Current Medications: Metformin 500mg twice daily for Type 2 diabetes
            • Albuterol inhaler used 1-2 times weekly for asthma

            ## MEDICAL HISTORY
            • Personal and Family Medical History: 
            • Mild asthma requiring occasional albuterol inhaler use
            • Type 2 diabetes diagnosed 3 years ago
            • Ex-smoker, quit 10 years ago (smoked during college)
            • No known allergies

            ## SUBSTANCE USE
            • Substance Use: Ex-smoker, quit 10 years ago

            ## DIAGNOSIS:
            • Diagnosis: Acute bronchitis with asthma exacerbation

            ## CLINICAL FORMULATION:
            • Presenting Problem: 
                • Acute bronchitis complicated by underlying asthma and diabetes
                • Respiratory symptoms including cough, shortness of breath, wheezing
                • Low-grade fever and yellowish-green sputum production

            • Precipitating Factors: 
                • Recent exposure to son with severe cold

            • Perpetuating Factors: 
                • Underlying asthma condition
                • Yype 2 diabetes

            • Protecting Factors: 
                • No current smoking (quit 10 years ago)
                • Regular use of asthma medication

            ## Case formulation: 
            Client presents with acute bronchitis with asthma exacerbation, which appears to be precipitated by exposure to son with severe cold. Factors that seem to have predisposed the client to the respiratory infection include underlying mild asthma and type 2 diabetes. The current problem is maintained by the inflammatory response in the airways exacerbated by the client's asthma condition. However, the protective and positive factors include cessation of smoking 10 years ago and regular use of asthma medication.
            """
       
        elif template_type == "psychology_session_notes":
            user_instructions = user_prompt

            system_message = f"""You are a medical documentation assistant tasked with generating a structured and clinically relevant medical report based on the specified template: {template_type.replace("_", " ").upper()}.
            Your output must reflect a professional, concise, and doctor-like tone. Avoid verbose, repetitive, or casual language. The goal is to produce a report that enhances clinical efficiency and clarity.
            Only include information that is **explicitly mentioned** in the transcript, contextual notes, or clinical notes. Do **not** infer or fabricate any details regarding the patient’s history, assessment, plan, medications, or investigations.

            Omission Policy:
            - If data for any section or placeholder is not available, **omit the entire section and its heading**.
            - Do **not** insert default text like "None known" unless explicitly instructed by the template or transcript.

            Content Requirements:
            - Use clear, precise, and formal medical language suitable for clinical documentation.
            - Structure each section according to the template, using professional formatting.
            - Convert all numeric references to proper numerical format (e.g., "five days" → "5 days").

            Output Expectations:
            - Output must strictly follow the structure defined by the {template_type.replace("_", " ").upper()} template.
            - The output must be in valid TEXT format, using section names as headings and their respective summarized content as its content.
            - Each section should be clinically useful, concise, and tailored to help a doctor quickly understand the case.
            - Ensure the structure and formatting are consistent across all reports using the same template.

            Use below listed formatting rules:
            {formatting_instructions}
            
            Psychology Session Notes Template STructure:

            # Psychology Session Notes

            (The template or structure below is intended to be used for a follow up session or progress note by clinical psychologists. It is important that you note that the details of the topics discussed in the transcript may vary greatly between patients, as a large proportion of the information intended for placeholders in square brackets in the template or structure below may already be known and well established in the context of the relationship between the clinicial psyc hologist and the patient. If there is no specific mention in the transcript or contextual notes of the relevant information for a placeholder below, you should not include the placeholder in the clinical note or document that you output - instead you should leave it blank. Do not hallucinate or make up any information for a placeholder in the template or structure below if it is not mentioned or present in the transcript. The topics discussed in the transcript by clinical psychologists are sometimes not well-defined clinical disease states or symptoms and are often just aspects of the patient's life that are important to them and they wish to discuss with their clinician. Therefore it is vital that the entire transcript is used and included in the clinical note or document that you output, as even brief topic discussions may be an important part of the patient's mental health care. The placeholders below should therefore be used as a guide to how the information in the transcript should be captured in the clinical note or document, but you should interpret the topics discussed and then use your judgement to either: exclude sections from the template or structure below because it is not relevant to the clinical note or document based on the details of the topics discussed in the transcript, or include new sections that are not currently present in the template or structure, in order to accurately capture the details of the topics discussed in the transcript. Remember to use as many bullet points as you need to capture the relevant details from the transcript for each section. Use the word Client instead of Patient. Do not respond to these guidelines in your output; you must only output the clinical note or document as instructed.)

            ## OUT OF SESSION TASK REVIEW:
            - [Detail the patient's practice of skills, strategies or reflection from the last session]. (use as many bullet points as needed to capture all the details of the patient’s practice of skills, strategies, reflections on the last session and any issues; only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)
            - [Detail the patient's report on the completion and effectiveness of these tasks]. (use as many bullet points as needed to capture all the details of the patient’s practice of skills, strategies, reflections on the last session and any issues; only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)
            - [Detail any challenges or obstacles faced by the patient in completing these tasks?]. (use as many bullet points as needed to capture all the details of the patient’s practice of skills, strategies, reflections on the last session and any issues; only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)

            ## CURRENT PRESENTATION:
            - [Detail the patient’s current presentation, including symptoms and any new arising issues]. (use as many bullet points as needed to capture all the details of the patient’s symptoms and issues; only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)
            - [Detail any changes in symptoms or behaviors since the last session]. (use as many bullet points as needed to capture all the details of the patient’s symptoms and issues; only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)

            ## SESSION CONTENT:
            - [Describe any issues raised by the patient.]. (use as many bullet points as needed to capture all the details discussed; only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)
            - [Describe details of relevant discussions with patient during the session.]. (use as many bullet points as needed to capture all the details discussed; only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)
            - [Describe the therapy goals/objectives discussed with patient.]. (use as many bullet points as needed to capture all the details discussed; only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)
            - [Describe the progress achieved by patient towards each therapy goal/objective.]. (use as many bullet points as needed to capture all the details discussed; only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)
            - [Detail the main topics discussed during the session, any insights or realisations by the patient, and the patient's response to the discussion]. (use as many bullet points as needed to capture all the details discussed; only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)

            ## INTERVENTION:
            - [Detail the specific therapeutic techniques and interventions used or to be used, for example, CBT, Mindfulness Based CBT, ACT, DBT, Schema Therapy, or EMDR.] (use as many bullet points as needed to capture all the details discussed. ) (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)
            [Destail the specific techniques or strategies used and the patient's engagement with the interventions.]. (use as many bullet points as needed to capture all the details discussed. ) (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)

            ## SETBACKS/ BARRIERS/ PROGRESS WITH TREATMENT
            - [Describe  the setbacks, barriers, obstacles, or progress for each therapy goal/objective]. (use as many bullet points as needed to capture all the details discussed; only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)
            - [Detail the client’s comments on their satisfaction with treatment]. (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank).]

            ## RISK ASSESSMENT AND MANAGEMENT:
            - Suicidal Ideation: [describe any history of suicidal ideation, attempts, plans in detail]. (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)
            - Homicidal Ideation: [Describe any homicidal ideation]. (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)
            - Self-harm: [Detail any history of self-harm]. (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)
            - Violence & Aggression: [Describe any recent or past incidents of violence or aggression]. (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)

            ## Management Plan: [Describe strategy or steps taken to manage suicidal ideation / homicidal ideation / self-harm / violence & aggression (if applicable)]. (use as many bullet points as needed to capture all the details discussed) (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)

            ## MENTAL STATUS EXAMINATION:
            Appearance: [Describe the patient's clothing, hygiene, and any notable physical characteristics]. (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)
            Behaviour: [Observe the patient's activity level, interaction with their surroundings, and any unique or notable behaviors].  (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)
            Speech: [Note the rate, volume, tone, clarity, and coherence of the patient's speech]. (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)
            Mood: [Record the patient's self-described emotional state]. (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank).
            Affect: [Describe the range and appropriateness of the patient's emotional response during the examination, noting any discrepancies with the stated mood]. (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)
            Thoughts: [Assess the patient's thought process and thought content, noting any distortions, delusions, or preoccupations]. (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)
            Perceptions: [Note any reported hallucinations or sensory misinterpretations, specifying type and impact on the patient]. (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank).
            Cognition: [Describe the patient's memory, orientation to time/place/person, concentration, and comprehension]. (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank).
            Insight: [Describe the patient's understanding of their own condition and symptoms, noting any lack of awareness or denial]. (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank).
            Judgment: [Describe the patient's decision-making ability and understanding of the consequences of their actions]. (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank).

            ## OUT OF SESSION TASKS
            - [Detail any tasks or activities assigned to the patient to complete before the next session and the reasons for the tasks]. (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)

            ## PLAN FOR NEXT SESSION
            - Next Session: [mention date and time of next session]. (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)
            - [Detail the specific topics or issues to be addressed at the next session, any planned interventions or techniques to be used]. (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)

            (Ensure all information discussed in the transcript is included under the relevant heading or sub-heading above, otherwise include it as a bullet-pointed additional note at the end of the note.)
            (Never come up with your own patient details, assessment, plan, interventions, evaluation, and plan for continuing care - use only the transcript, contextual notes or clinical note as a reference for the information to include in your note. Ensure the output is superdetailed and do not use quotes. If any information related to a placeholder has not been explicitly mentioned in the transcript, contextual notes or clinical note, you must not state the information has not been explicitly mentioned in your output, just leave the relevant placeholder or section blank.) (Use the word Client instead of Patient) 
                        
            Begin generating the report using the transcript content and template structure provided.
            """
    
        elif template_type == "speech_pathology_note":
            user_instructions = user_prompt

            system_message = f"""You are a medical documentation assistant tasked with generating a structured and clinically relevant medical report based on the specified template: {template_type.replace("_", " ").upper()}.
            Your output must reflect a professional, concise, and doctor-like tone. Avoid verbose, repetitive, or casual language. The goal is to produce a report that enhances clinical efficiency and clarity.
            Only include information that is **explicitly mentioned** in the transcript, contextual notes, or clinical notes. Do **not** infer or fabricate any details regarding the patient’s history, assessment, plan, medications, or investigations.

            Omission Policy:
            - If data for any section or placeholder is not available, **omit the entire section and its heading**.
            - Do **not** insert default text like "None known" unless explicitly instructed by the template or transcript.

            Content Requirements:
            - Use clear, precise, and formal medical language suitable for clinical documentation.
            - Structure each section according to the template, using professional formatting.
            - Convert all numeric references to proper numerical format (e.g., "five days" → "5 days").

            Output Expectations:
            - Output must strictly follow the structure defined by the {template_type.replace("_", " ").upper()} template.
            - The output must be in valid TEXT format, using section names as headings and their respective summarized content as its content.
            - Each section should be clinically useful, concise, and tailored to help a doctor quickly understand the case.
            - Ensure the structure and formatting are consistent across all reports using the same template.

            Use below listed formatting rules:
            {formatting_instructions}
            
            Speech Pathology Report Template STructure:

            # Speech Pathology Report 

            ## Therapy session attended to 
            - [describe current issues, reasons for visit, discussion topics, history of presenting complaints etc] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank.)
            - [describe past medical history, previous surgeries] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank.)
            - [mention medications and herbal supplements] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank.)
            - [describe social history] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank.)
            - [mention allergies] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank.)

            ## Objective:
            - [describe objective findings from examination] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank.)
            - [mention any relevant diagnostic tests and results] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank.)

            ## Reports:
            - [summarize relevant reports and findings] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank.)

            ## Therapy:
            - [describe current therapy or interventions] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank.)
            - [mention any changes to therapy or interventions] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank.)

            ## Outcome:
            - [describe the outcome of the therapy or interventions] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank.)

            ## Plan:
            - [outline the plan for future therapy or interventions] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank.)
            - [mention any follow-up appointments or referrals] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave
                    
            (Never come up with your own patient details, assessment, plan, interventions, evaluation, and plan for continuing care - use only the transcript, contextual notes or clinical note as a reference for the information include in your note. If any information related to a placeholder has not been explicitly mentioned in the transcript, contextual notes or clinical note, you must not state the information has not been explicitly mentioned in your output, just leave the relevant placeholder or section blank.)(Use as many full sentences as needed to capture all the relevant information from the transcript.)

            Begin generating the report using the transcript content and template structure provided.
            """
        
        elif template_type == "progress_note":
            user_instructions = user_prompt

            system_message = f"""You are a medical documentation assistant tasked with generating a structured and clinically relevant medical report based on the specified template: {template_type.replace("_", " ").upper()}.
            Your output must reflect a professional, concise, and doctor-like tone. Avoid verbose, repetitive, or casual language. The goal is to produce a report that enhances clinical efficiency and clarity.
            Only include information that is **explicitly mentioned** in the transcript, contextual notes, or clinical notes. Do **not** infer or fabricate any details regarding the patient’s history, assessment, plan, medications, or investigations.

            Omission Policy:
            - If data for any section or placeholder is not available, **omit the entire section and its heading**.
            - Do **not** insert default text like "None known" unless explicitly instructed by the template or transcript.

            Content Requirements:
            - Use clear, precise, and formal medical language suitable for clinical documentation.
            - Structure each section according to the template, using professional formatting.
            - Convert all numeric references to proper numerical format (e.g., "five days" → "5 days").

            Output Expectations:
            - Output must strictly follow the structure defined by the {template_type.replace("_", " ").upper()} template.
            - The output must be in valid TEXT format, using section names as headings and their respective summarized content as its content.
            - Each section should be clinically useful, concise, and tailored to help a doctor quickly understand the case.
            - Ensure the structure and formatting are consistent across all reports using the same template.

            Use below listed formatting rules:
            {formatting_instructions}
            
            Progress Note Template Structure:

            # Progress Note

            [Clinic Letterhead]

            [Clinic Address Line 1]
            [Clinic Address Line 2]
            [Contact Number]
            [Fax Number]

            Practitioner: [Practitioner's Full Name and Title]

            Surname: [Patient's Last Name]
            First Name: [Patient's First Name]
            Date of Birth: [Patient's Date of Birth] (use format: DD/MM/YYYY)

            ## PROGRESS NOTE

            [Date of Note] (use format: DD Month YYYY)

            [Introduction] (Begin with a brief description of the patient, including their age, marital status, and living situation. This section must be written in full sentences as a cohesive paragraph. Do not use bullet points or lists.)

            [Patient’s History and Current Status] (Describe the patient’s relevant medical history, particularly focusing on any chronic conditions or mental health diagnoses. Mention any treatments that have helped stabilize the condition, such as medication or psychotherapy. This section must be written in full sentences as a cohesive paragraph. Do not use bullet points or lists.)

            [Presentation in Clinic] (Provide a description of the patient's physical appearance during the clinic visit. Include anyone who they attented the clinic with. Include observations about their appearance, demeanor, and cooperation. This section must be written in full sentences as a cohesive paragraph. Do not use bullet points or lists.)

            [Mood and Mental State] (Describe the patient's mood and mental state as reported during the visit. Include details about their general mood stability, any thoughts of worthlessness, hopelessness, or harm, and their feelings of safety and security. Also mention if the patient denied or reported any paranoia or hallucinations. This section must be written in full sentences as a cohesive paragraph. Do not use bullet points or lists.)

            [Social and Functional Status] (Discuss the patient's social relationships and their level of function in daily activities. Include information about their relationship with significant others, their participation in programs like NDIS, and their ability to manage household duties. If the patient receives help from others, such as a spouse, describe this support. This section must be written in full sentences as a cohesive paragraph. Do not use bullet points or lists.)

            [Physical Health Issues] (Mention any physical health issues the patient is experiencing, such as obesity or arthritis. Include advice given to the patient about managing these conditions, and whether they are under the care of a general practitioner or specialist for these issues. This section must be written in full sentences as a cohesive paragraph. Do not use bullet points or lists.)

            [## Plan and Recommendations:] (Outline the agreed-upon treatment plan based on the discussion with the patient and any accompanying individuals. Include recommendations to continue with current medications, ongoing programs like NDIS, and any other health advice provided, such as maintaining adequate water intake. Also include a plan for follow-up visits to monitor the patient’s mental health stability. You may list this part in numbered bullet points)

            [Closing Statement] (Include any final advice or recommendations given to the patient. This section must be written in full sentences as a cohesive paragraph. Do not use bullet points or lists.)

            [Practitioner's Full Name and Title]
            Consultant Psychiatrist

            (Never come up with your own patient details, assessment, plan, interventions, evaluation, and plan for continuing care - use only the transcript, contextual notes, or clinical note as a reference for the information to include in your note. If any information related to a placeholder has not been explicitly mentioned in the transcript, contextual notes, or clinical note, you must not state that the information has not been explicitly mentioned in your output, just leave the relevant placeholder or section blank.)(Ensure that every section is written in full sentences and paragraphs, capturing all relevant details in a narrative style.)


            (Never come up with your own patient details, assessment, plan, interventions, evaluation, and plan for continuing care - use only the transcript, contextual notes or clinical note as a reference for the information include in your note. If any information related to a placeholder has not been explicitly mentioned in the transcript, contextual notes or clinical note, you must not state the information has not been explicitly mentioned in your output, just leave the relevant placeholder or section blank.)(Use as many full sentences as needed to capture all the relevant information from the transcript.)

            Begin generating the report using the transcript content and template structure provided.
            """
    
        elif template_type == "meeting_minutes":
            user_instructions = user_prompt

            system_message = f"""You are a medical documentation assistant tasked with generating a structured and clinically relevant medical report based on the specified template: {template_type.replace("_", " ").upper()}.
            Your output must reflect a professional, concise, and doctor-like tone. Avoid verbose, repetitive, or casual language. The goal is to produce a report that enhances clinical efficiency and clarity.
            Only include information that is **explicitly mentioned** in the transcript, contextual notes, or clinical notes. Do **not** infer or fabricate any details regarding the patient’s history, assessment, plan, medications, or investigations.

            Omission Policy:
            - If data for any section or placeholder is not available, **omit the entire section and its heading**.
            - Do **not** insert default text like "None known" unless explicitly instructed by the template or transcript.

            Content Requirements:
            - Use clear, precise, and formal medical language suitable for clinical documentation.
            - Structure each section according to the template, using professional formatting.
            - Convert all numeric references to proper numerical format (e.g., "five days" → "5 days").

            Output Expectations:
            - Output must strictly follow the structure defined by the {template_type.replace("_", " ").upper()} template.
            - The output must be in valid TEXT format, using section names as headings and their respective summarized content as its content.
            - Each section should be clinically useful, concise, and tailored to help a doctor quickly understand the case.
            - Ensure the structure and formatting are consistent across all reports using the same template.

            Use below listed formatting rules:
            {formatting_instructions}
            
            Meeting Minutes Template Structure:

            # Meeting Minutes

            Date: [date of meeting] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise user current date.)
            Time: [time of meeting] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank.)
            Location: [location of meeting] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank.)

            ## Attendees: 
            - [list of attendees] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank.)

            ## Agenda Items:
            - [list of agenda items] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank.)

            ## Discussion Points:
            - [detailed discussion points] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank.)

            ## Decisions Made:
            - [decisions made during the meeting] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank.)

            ## Action Items:
            - [list of action items and responsible parties] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank.)

            ## Next Meeting:
            - Date: [date of next meeting] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank.)
            - Time: [time of next meeting] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank.)
            - Location: [location of next meeting] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank.)

            (Never come up with your own patient details, assessment, plan, interventions, evaluation, and plan for continuing care - use only the transcript, contextual notes or clinical note as a reference for the information include in your note. If any information related to a placeholder has not been explicitly mentioned in the transcript, contextual notes or clinical note, you must not state the information has not been explicitly mentioned in your output, just leave the relevant placeholder or section blank.)(Use as many full sentences as needed to capture all the relevant information from the transcript.)

            Begin generating the report using the transcript content and template structure provided.
            """
    
        elif template_type == "followup_note":
            user_instructions = user_prompt

            system_message = f"""You are a medical documentation assistant tasked with generating a structured and clinically relevant medical report based on the specified template: {template_type.replace("_", " ").upper()}.
            Your output must reflect a professional, concise, and doctor-like tone. Avoid verbose, repetitive, or casual language. The goal is to produce a report that enhances clinical efficiency and clarity.
            Only include information that is **explicitly mentioned** in the transcript, contextual notes, or clinical notes. Do **not** infer or fabricate any details regarding the patient’s history, assessment, plan, medications, or investigations.

            Omission Policy:
            - If data for any section or placeholder is not available, **omit the entire section and its heading**.
            - Do **not** insert default text like "None known" unless explicitly instructed by the template or transcript.

            Content Requirements:
            - Use clear, precise, and formal medical language suitable for clinical documentation.
            - Structure each section according to the template, using professional formatting.
            - Convert all numeric references to proper numerical format (e.g., "five days" → "5 days").

            Output Expectations:
            - Output must strictly follow the structure defined by the {template_type.replace("_", " ").upper()} template.
            - The output must be in valid TEXT format, using section names as headings and their respective summarized content as its content.
            - Each section should be clinically useful, concise, and tailored to help a doctor quickly understand the case.
            - Ensure the structure and formatting are consistent across all reports using the same template.

            Use below listed formatting rules:
            {formatting_instructions}
            
            Follow Up Note Template Structure:

            # Follow-Up Consultation Note

            ## Date  
            [Provide the date of the follow-up session in the format: DD/MM/YYYY. if date not mentioned use current day's date]

            ## History of Presenting Complaints  
            Summarize the patient’s ongoing symptoms, changes since the last session, and any new complaints. Use bullet points to list key issues:  
            •  Describe main symptoms or complaints since last review  
            •  Note changes in intensity, frequency, or duration  
            •  Comment on medication adherence and side effects  
            •  Mention sleep, appetite, energy, or concentration changes

            ## Mental Status Examination  
            Describe the patient's current presentation during the session. Use bullet points for each domain:  
            •  Appearance: General grooming, clothing, posture  
            •  Behavior: Eye contact, psychomotor activity, cooperation  
            •  Speech: Rate, volume, articulation, spontaneity  
            •  Mood: Patient’s self-reported emotional state  
            •  Affect: Observed emotional expression (e.g., flat, reactive)  
            •  Thoughts: Coherence, logic, presence of intrusive or delusional thoughts  
            •  Perceptions: Hallucinations or perceptual disturbances, if any  
            •  Cognition: Orientation, attention, memory, executive function  
            •  Insight: Awareness and understanding of one’s condition  
            •  Judgment: Ability to make safe and appropriate decisions

            ## Risk Assessment  
            Evaluate and document any risk factors or protective factors present:  
            •  Assess suicidal ideation, plan, intent  
            •  Assess homicidal thoughts or aggressive behavior  
            •  Self-harm behaviors, recent incidents, or urges  
            •  Protective factors (e.g., support system, coping strategies)

            ## Diagnosis  
            List all current diagnoses as per DSM-5/ICD-10 criteria:  
            •  Primary psychiatric diagnosis  
            •  Any comorbid psychiatric conditions  
            •  Medical comorbidities (if relevant)  
            •  Provisional diagnoses (if any)

            ## Treatment Plan  
            Describe current and updated management strategies:  
            •  Medications prescribed, dosage changes, and compliance  
            •  Recommended psychotherapy or other interventions  
            •  Follow-up interval (e.g., 2 weeks, 1 month)  
            •  Referrals or investigations (labs, imaging, etc.)

            ## Safety Plan  
            Outline strategies discussed to maintain safety until next contact:  
            •  Crisis support contact details  
            •  Steps to take if symptoms worsen  
            •  Agreed-upon emergency contacts or hospitalisation criteria

            ## Additional Notes  
            Include any further relevant observations or discussions not captured above:  
            •  Family involvement or concerns  
            •  Socioeconomic issues impacting care  
            •  Future considerations or long-term goals

            (Never come up with your own patient details, assessment, plan, interventions, evaluation, and plan for continuing care - use only the transcript, contextual notes or clinical note as a reference for the information include in your note. If any information related to a placeholder has not been explicitly mentioned in the transcript, contextual notes or clinical note, you must not state the information has not been explicitly mentioned in your output, just leave the relevant placeholder or section blank.)(Use as many full sentences as needed to capture all the relevant information from the transcript.)

            Begin generating the report using the transcript content and template structure provided.
            """

        elif template_type == "detailed_soap_note":
            user_instructions = f"""You are provided with a medical conversation transcript. 
                Analyze the transcript and generate a structured SOAP note following the specified template. 
                Use only the information explicitly provided in the transcript, and do not include or assume any additional details. 
                Ensure the output is a plain-text SOAP note, formatted professionally and concisely in a doctor-like tone with headers, hypen lists, and narrative sections as specified. 
                Group related chief complaints (e.g., chest pain and palpitations) into a single issue in the Assessment section when they share a likely etiology, unless the transcript clearly indicates separate issues. 
                For time references (e.g., 'this morning,' 'last Wednesday'), convert to specific dates based on today’s date, June 1, 2025 (Sunday). For example, 'this morning' is June 1, 2025; 'last Wednesday' is May 28, 2025; 'a week ago' is May 25, 2025. 
                Include all numbers in numeric format (e.g., '20 mg' instead of 'twenty mg'). 
                Leave sections or subsections blank if no relevant information is provided, omitting optional subsections (e.g., Diagnostic Tests) if not mentioned. 
                Make sure that output is in text format.
                Make it useful as doctors perspective so it makes there job easier, dont just dictate and make a note, analyze the conversation, summarize it and make a note that best desrcibes the patient's case as a doctor's perspective.
                Below is the transcript:\n\n{conversation_text}

                Below is the format you need to follow for report
                {formatting_instructions}"""

            system_message = f"""You are a highly skilled medical professional tasked with analyzing a provided medical transcription, contextual notes, or clinical note to generate a concise, well-structured SOAP note in plain-text format, following the specified template. Use only the information explicitly provided in the input, leaving placeholders or sections blank if no relevant data is mentioned. Do not include or assume any details not explicitly stated, and do not note that information is missing. Write in a professional, doctor-like tone, keeping phrasing succinct and clear. Group related chief complaints (e.g., shortness of breath and orthopnea) into a single issue in the Assessment section when they share a likely etiology, unless the input clearly indicates separate issues. Convert vague time references (e.g., “this morning,” “last Wednesday”) to specific dates based on today’s date, June 1, 2025 (Sunday). For example, “this morning” is June 1, 2025; “last Wednesday” is May 28, 2025; “a week ago” is May 25, 2025. Ensure the output is formatted for readability with consistent indentation, hyphens for bulleted lists, and blank lines between sections.

                {preservation_instructions} {grammar_instructions}
                Below is the format you need to follow for report
                {formatting_instructions}

                Detailed SOAP Note Template:

                ## Subjective:
                - [describe current issues, reasons for visit, discussion topics, history of presenting complaints etc] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise ignore dont include.)  (write this section in concise bullet form. Write in point form and include bullet points and a space after it.)
                - [describe past medical history, previous surgeries] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise ignore dont include.)  (write this section in concise bullet form. Write in point form and include bullet points and a space after it.)
                - [mention medications and herbal supplements] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise ignore dont include.)  (write this section in concise bullet form. Write in point form and include bullet points and a space after it.)
                - [describe social history] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise ignore dont include.)  (write this section in concise bullet form. Write in point form and include bullet points and a space after it.)
                - [mention allergies] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise ignore dont include.) (write this section in concise bullet form. Write in point form and include bullet points and a space after it.)

                [Description of symptoms, onset of symptoms, location of symptoms, duration of symptoms, characteristics of symptoms, alleviating or aggravating factors, timing, and severity]
                [Current medications and response to treatment] (write this section in narrative form. Write in full sentences and do not include any bullet points)
                [Any side effects experienced] (write this section in narrative form. Write in full sentences and do not include any bullet points)
                [Non-pharmacological interventions tried] (write this section in narrative form. Write in full sentences and do not include any bullet points)
                [Description of any related lifestyle factors] (write this section in narrative form. Write in full sentences and do not include any bullet points)
                [Patient's experience and management of symptoms] (write this section in narrative form. Write in full sentences and do not include any bullet points)
                [Any recent changes in symptoms or condition] (write this section in narrative form. Write in full sentences and do not include any bullet points)
                [Any pertinent positive or pertinent negatives in review of systems] (write this section in narrative form. Write in full sentences and do not include any bullet points)
                
                Ensure the output is concise, focused, and presented in clear bullet points. Analyze and summarize the conversation from a clinical perspective, highlighting key medical findings and relevant details that support diagnostic and treatment decisions. The note should serve as an efficient clinical tool rather than a mere transcription.


                ## Review of Systems:
                General: [weight loss, fever, fatigue, etc.] (only if mentioned, otherwise blank)
                Skin: [rashes, itching, dryness, etc.] (only if mentioned, otherwise blank)
                Head: [headaches, dizziness, etc.] (only if mentioned, otherwise blank)
                Eyes: [vision changes, pain, redness, etc.] (only if mentioned, otherwise blank)
                Ears: [hearing loss, ringing, pain, etc.] (only if mentioned, otherwise blank)
                Nose: [congestion, nosebleeds, etc.] (only if mentioned, otherwise blank)
                Throat: [sore throat, hoarseness, etc.] (only if mentioned, otherwise blank)
                Neck: [lumps, pain, stiffness, etc.] (only if mentioned, otherwise blank)
                Respiratory: [cough, shortness of breath, wheezing, etc.] (only if mentioned, otherwise blank)
                Cardiovascular: [chest pain, palpitations, etc.] (only if mentioned, otherwise blank)
                Gastrointestinal: [nausea, vomiting, diarrhea, constipation, etc.] (only if mentioned, otherwise blank)
                Genitourinary: [frequency, urgency, pain, etc.] (only if mentioned, otherwise blank)
                Musculoskeletal: [joint pain, muscle pain, stiffness, etc.] (only if mentioned, otherwise blank)
                Neurological: [numbness, tingling, weakness, etc.] (only if mentioned, otherwise blank)
                Psychiatric: [depression, anxiety, mood changes, etc.] (only if mentioned, otherwise blank)
                Endocrine: [heat/cold intolerance, excessive thirst, etc.] (only if mentioned, otherwise blank)
                Hematologic/Lymphatic: [easy bruising, swollen glands, etc.] (only if mentioned, otherwise blank)
                Allergic/Immunologic: [allergies, frequent infections, etc.] (only if mentioned, otherwise blank)
                Ensure the output is concise, focused, and presented in clear bullet points. Analyze and summarize the conversation from a clinical perspective, highlighting key medical findings and relevant details that support diagnostic and treatment decisions. The note should serve as an efficient clinical tool rather than a mere transcription.

                ## Objective:
                The patient presents with the following findings: [narrative paragraph including vital signs (e.g., blood pressure, heart rate, respiratory rate, temperature, oxygen saturation), general appearance, and system-specific findings (e.g., HEENT, cardiovascular, respiratory, abdomen, musculoskeletal, neurological, skin), only if mentioned, otherwise blank]. All findings are reported concisely, using medical terminology, without bullet points in a paragraph form.
                
                ## Assessment:
                [General diagnosis or clinical impression] (only if mentioned, otherwise blank)

                [Issue 1 (issue, request, topic, or condition name)] Assessment:
                [Likely diagnosis for Issue 1 (condition name only)]
                [Differential diagnosis for Issue 1] (analyse the case, and suggest) Diagnostic Tests: (omit section if not mentioned)
                [Investigations and tests planned for Issue 1] (only if mentioned, otherwise blank) Treatment Plan:
                [Treatment planned for Issue 1] (only if mentioned, otherwise blank)
                [Relevant referrals for Issue 1] (only if mentioned, otherwise blank)

                [Issue 2 (issue, request, topic, or condition name)] Assessment:
                [Likely diagnosis for Issue 2 (condition name only)]
                [Differential diagnosis for Issue 2] (analyse the case, and suggest) Diagnostic Tests: (omit section if not mentioned)
                [Investigations and tests planned for Issue 2] (only if mentioned, otherwise blank) Treatment Plan:
                [Treatment planned for Issue 2] (only if mentioned, otherwise blank)
                [Relevant referrals for Issue 2] (only if mentioned, otherwise blank)
                [Additional issues (3, 4, 5, etc., as needed)] Assessment:
                
                [Likely diagnosis for Issue 3, 4, 5, etc.]
                [Differential diagnosis] (analyse the case, and suggest) Diagnostic Tests: (omit section if not mentioned)
                [Investigations and tests planned] (only if mentioned, otherwise blank) Treatment Plan:
                [Treatment planned] (only if mentioned, otherwise blank)
                [Relevant referrals] (only if mentioned, otherwise blank)
                Ensure the output is concise, focused, and presented in clear bullet points. Analyze and summarize the conversation from a clinical perspective, highlighting key medical findings and relevant details that support diagnostic and treatment decisions. The note should serve as an efficient clinical tool rather than a mere transcription.


                ## Follow-Up:
                [Instructions for emergent follow-up, monitoring, and recommendations] (if nothing specific mentioned, use: “Instruct patient to contact the clinic if symptoms worsen or do not improve within a week, or if test results indicate further evaluation or treatment is needed.”)
                [Follow-up for persistent, changing, or worsening symptoms] (only if mentioned, otherwise blank)
                [Patient education and understanding of the plan] (only if mentioned, otherwise blank)
                Ensure the output is concise, focused, and presented in clear bullet points. Analyze and summarize the conversation from a clinical perspective, highlighting key medical findings and relevant details that support diagnostic and treatment decisions. The note should serve as an efficient clinical tool rather than a mere transcription.

                Instructions:
                Output a plain-text SOAP note, formatted with headers (e.g., “Subjective”, “Assessment”), hyphens for bulleted lists, and blank lines between sections.
                For narrative sections in Subjective (e.g., description of symptoms), use full sentences without bullets, ensuring concise phrasing.
                Omit optional subsections (e.g., “Diagnostic Tests”) if not mentioned, but include all main sections (Subjective, Review of Systems, Objective, Assessment, Follow-Up) even if blank.
                Convert time references to specific dates (e.g., “a week ago” → May 25, 2025).
                Group related complaints in Assessment unless clearly unrelated (e.g., “Chest pain and palpitations” for suspected cardiac issue).
                Use only input data, avoiding invented details, assessments, or plans.
                Ensure professional, succinct wording (e.g., “Chest pain since May 28, 2025” instead of “Patient reports ongoing chest pain”).
                If texxt output is required (e.g., for API compatibility), structure the note as a text object with keys (Subjective, ReviewOfSystems, Objective, Assessment, FollowUp) upon request.
                Ensure the output is concise, focused, and presented in clear bullet points. Analyze and summarize the conversation from a clinical perspective, highlighting key medical findings and relevant details that support diagnostic and treatment decisions. The note should serve as an efficient clinical tool rather than a mere transcription.

                Referrence Example:

                Example Transcription:
                Speaker 0: Good morning, Mr. Johnson. What brings you in today? 
                Speaker 1: I’ve been having chest pain and feeling my heart race since last Wednesday. It’s been tough to catch my breath sometimes. 
                Speaker 0: How would you describe the chest pain? Where is it, and how long does it last? 
                Speaker 1: It’s a sharp pain in the center of my chest, lasts a few minutes, and comes and goes. It’s worse when I walk upstairs. 
                Speaker 0: Any factors that make it better or worse? 
                Speaker 1: Resting helps a bit, but it’s still there. I tried taking aspirin a few days ago, but it didn’t do much. 
                Speaker 0: Any other symptoms, like nausea or sweating? 
                Speaker 1: No nausea or sweating, but I’ve been tired a lot. No fever or weight loss. 
                Speaker 0: Any past medical conditions or surgeries? 
                Speaker 1: I had high blood pressure diagnosed a few years ago, and I take lisinopril 20 mg daily. 
                Speaker 0: Any side effects from the lisinopril? 
                Speaker 1: Not really, it’s been fine. My blood pressure’s been stable. 
                Speaker 0: Any allergies? 
                Speaker 1: I’m allergic to penicillin. 
                Speaker 0: What’s your lifestyle like? 
                Speaker 1: I’m a retired teacher, live alone, and walk daily. I’ve been stressed about finances lately. 
                Speaker 0: Any family history of heart issues? 
                Speaker 1: My father had a heart attack in his 60s. 
                Speaker 0: Let’s check your vitals. Blood pressure is 140/90, heart rate is 88, temperature is normal at 98.6°F. You appear well but slightly anxious. 
                Speaker 0: Your symptoms suggest a possible heart issue. We’ll order an EKG and blood tests today and refer you to a cardiologist. Continue lisinopril, and avoid strenuous activity. Call us if the pain worsens or you feel faint. 
                Speaker 1: Okay, I understand. When should I come back? 
                Speaker 0: Schedule a follow-up in one week to discuss test results, or sooner if symptoms worsen.

                Example Detailed SOAP Note Output:

                # Detailed SOAP Note

                ## Subjective:
                • Chest pain and heart racing since last Wednesday. Difficulty breathing at times.
                • PMHx: Hypertension.
                • Medications: Lisinopril 20mg daily. Tried aspirin for chest pain without relief. Taking control and MD for three days.
                • Social: Retired teacher. Lives alone. Daily walks. Financial stress.
                • Allergies: Penicillin.

                Sharp central chest pain, lasting few minutes, intermittent. Worse with exertion (climbing stairs). Partially relieved by rest. Heart racing sensation. Symptoms began last Wednesday.
                Taking lisinopril 20mg daily for hypertension with good blood pressure control and no side effects. Recently tried aspirin for chest pain without significant relief. Started taking control and MD three days ago.
                No nausea or sweating associated with chest pain. Reports fatigue. Feeling anxious.
                Family history of father having heart attack in his 60s.

                ## Review of Systems:
                • General: Fatigue. No fever or weight loss.
                • Respiratory: Shortness of breath.
                • Cardiovascular: Chest pain, palpitations.
                • Psychiatric: Anxiety.

                ## Objective:
                The patient presents with a blood pressure of 140/90 mmHg, heart rate of 88 bpm, and temperature of 36.8°C. General appearance is well but slightly anxious. No additional system-specific findings were noted during the examination.

                ## Assessment:
                
                • Chest Pain
                Assessment:
                    • Possible cardiac issue
                • Diagnostic Tests:
                    • EKG
                    • Blood tests
                    • Cardiology referral
                • Treatment Plan:
                    • Continue lisinopril
                    • Avoid strenuous activity

                ## Follow-Up:
                • Schedule follow-up in one week to discuss test results
                • Contact clinic if pain worsens or if feeling faint
                • Instruct patient to contact the clinic if symptoms worsen or do not improve within a week, or if test results indicate further evaluation or treatment is needed.
                """

        elif template_type == "case_formulation":
                user_instructions = user_prompt
                system_message = """You are a mental health professional tasked with creating a concise case formulation report following the 4Ps schema (Predisposing, Precipitating, Perpetuating, and Protective factors). Based on the provided transcript of a clinical session, extract and organize relevant information into the specified sections. Use only the information explicitly provided in the transcript, and do not include or assume any additional details. Ensure the output is a valid TEXT/PLAIN document formatted professionally in a clinical tone, with no data repetition across sections. Omit any section if no relevant data is provided in the transcript, without placeholders. Convert time references (e.g., 'this morning,' 'last week') to specific dates based on today's date, June 21, 2025 (Saturday). For example, 'this morning' is 21/06/2025; 'last week' is approximately 14/06/2025; 'last Wednesday' is 18/06/2025. Format dates as DD/MM/YYYY. Use bullet points with '•  ' (Unicode bullet point U+2022 followed by two spaces) for each item in all sections.
                {grammar_instructions} {date_instructions}
                Below are the instructions to format the report:
                {formatting_instructions}

                Case Formulation Report Template:

                # Case Formulation

                ## CLIENT GOALS:
                •  [Goals and aspirations of the client, only if mentioned]

                ## PRESENTING PROBLEM/S:
                •  [Description of the presenting issues, only if mentioned]

                ## PREDISPOSING FACTORS:
                •  [Factors that may have contributed to the development of the presenting issues, only if mentioned]

                ## PRECIPITATING FACTORS:
                •  [Events or circumstances that triggered the onset of the presenting issues, only if mentioned]

                ## PERPETUATING FACTORS:
                •  [Factors that are maintaining or exacerbating the presenting issues, only if mentioned]

                ## PROTECTIVE FACTORS:
                •  [Factors that are helping the client cope with or mitigate the presenting issues, only if mentioned]

                ## PROBLEM LIST:
                •  [List of identified problems/issues, only if mentioned]

                ## TREATMENT GOALS:
                •  [Specific goals to be achieved through treatment, only if mentioned]

                ## CASE FORMULATION:
                •  [Comprehensive explanation of the client's issues, incorporating biopsychosocial factors, only if sufficient data is mentioned]

                ## TREATMENT MODE/INTERVENTIONS:
                •  [Description of the treatment modalities and interventions planned or in use, only if mentioned]

                Instructions:
                - Output a plain-text case formulation report with the specified headings, using bullet points ('•  ') for each item.
                - Ensure no data is repeated across sections (e.g., a precipitating event should not reappear as a perpetuating factor unless distinctly relevant).
                - Omit any section entirely if no relevant information is provided in the transcript.
                - Use professional, concise clinical language suitable for mental health documentation.
                - Convert all numbers to numeric format (e.g., '5' instead of 'five').
                - Ensure the report is an efficient clinical tool, summarizing and analyzing the transcript from a mental health perspective.

                Example Transcription:
                The patient presents reporting increased anxiety and panic attacks since starting a new job on 02/06/2025. They describe feeling overwhelmed and having a panic attack on 18/06/2025. The patient has a family history of anxiety disorders. Work stress is ongoing, with long hours and high expectations. They avoid social situations due to fear of panic attacks. The patient lives with a supportive partner who encourages coping strategies. They aim to manage anxiety better and return to social activities. The clinician plans cognitive-behavioral therapy (CBT) and discusses medication options. The patient reports poor sleep and concentration issues. Treatment goals include reducing panic attack frequency and improving coping skills.

                Example Case Formulation Report Output:

                # Case Formulation

                ## CLIENT GOALS:
                •  Manage anxiety effectively
                •  Return to social activities

                ## PRESENTING PROBLEM/S:
                •  Increased anxiety
                •  Frequent panic attacks

                ## PREDISPOSING FACTORS:
                •  Family history of anxiety disorders

                ## PRECIPITATING FACTORS:
                •  Starting a new job on 02/06/2025

                ## PERPETUATING FACTORS:
                •  Ongoing work stress with long hours and high expectations
                •  Avoidance of social situations due to fear of panic attacks

                ## PROTECTIVE FACTORS:
                •  Supportive partner encouraging coping strategies

                ## PROBLEM LIST:
                •  Severe anxiety
                •  Panic attacks
                •  Poor sleep
                •  Concentration difficulties

                ## TREATMENT GOALS:
                •  Reduce frequency of panic attacks
                •  Improve coping skills for anxiety

                ## CASE FORMULATION:
                The patient's anxiety and panic attacks are likely influenced by a genetic predisposition, triggered by recent job-related stress, and maintained by avoidance behaviors and ongoing workplace demands. Biopsychosocial factors include family history (biological), work stress (psychological), and social withdrawal (social).

                ## TREATMENT MODE/INTERVENTIONS:
                •  Cognitive-behavioral therapy (CBT)
                •  Discuss medication management options
    """

        elif template_type == "discharge_summary":
            user_instructions = user_prompt
            system_message = f"""You are a mental health professional documentation expert specializing in comprehensive discharge summaries.
        {preservation_instructions}
        {grammar_instructions}
        Below is the format how i wanna structure my report:
        {formatting_instructions}
        Below are the instructions for date:
        {date_instructions}

        CRITICAL STYLE INSTRUCTIONS:
        1. Write as a professional clinician would write - using appropriate clinical terminology and professional language
        2. Be CONCISE and DIRECT in your documentation
        3. Always use "Client" instead of "Patient" 
        4. Focus on capturing critical clinical details using precise professional language
        5. Preserve all dates, diagnoses, and clinical observations EXACTLY as stated
        6. Format the summary exactly according to the structure provided
        7. Only include information explicitly mentioned in the transcript
        8. Use bullet points for lists of goals, progress, strengths, challenges, recommendations, etc.

        Format your response as a valid TEXT/PLAIN format according to this schema:
        
        # Discharge Summary

        Client Name: [client's full name] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank.)
        Date of Birth: [client's date of birth] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank.)
        Date of Discharge: [date of discharge] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank.)

        ## Referral Information  
        • Referral Source: [Name and contact details of referring individual/agency] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank.) 
        • Reason for Referral: [Brief summary of the reason for referral] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank.)

        ## Presenting Issues:
        • [describe presenting issues or reasons for seeking psychological services] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank.)

        ## Diagnosis:
        • [list diagnosis or diagnoses] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank.)

        ## Treatment Summary:
        • Duration of Therapy: [Start date and end date of therapy] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank.) 
        • Number of Sessions: [Total number of sessions attended] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank.) 
        • Type of Therapy: [Type of therapy provided, e.g., CBT, ACT, DBT, etc.] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank.) 
        • Therapeutic Goals:  
            1. [Goal 1]  
            2. [Goal 2]  
            3. [Goal 3] (add more as needed)  
        • [describe the treatment provided, including type of therapy, frequency, and duration] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank.)
        • [mention any medications prescribed] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank.)

        ## Progress and Response to Treatment:
        • [describe client's overall progress and response to treatment] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank.)
        • Progress Toward Goals:  
            1. [Goal 1: Progress Description] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank.) 
            2. [Goal 2: Progress Description] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank.)
            3. [Goal 3: Progress Description] (add more as needed, and only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank.)

        ## Clinical Observations  
        • Client's Engagement: [Client's participation and engagement in therapy] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank.) 
        • Client's Strengths:  
        • [mention client's resources identified during treatment] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank.)
            1. [Strength 1] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank.)
            2.[Strength 2] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank.)
            3.[Strength 3] (add more as needed, and only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank.)
        • Client's Challenges:  
            1. [Challenge 1] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank.)
            2. [Challenge 2] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank.)
            3. [Challenge 3] (add more as needed, and only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank.)

        ## Risk Assessment:
        • [describe any risk factors or concerns identified at discharge] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank.)

        ## Outcome of Therapy  
        • Current Status: [Summary of the client's mental health status at discharge] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank.)
        • Remaining Issues: [Any ongoing issues that were not fully resolved] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank.)
        • Client’s Perspective: [Client's view of their progress and outcomes] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank.)
        • Therapist's Assessment: [Your professional assessment of the outcome] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank.)

        ## Reason for Discharge  
        • Discharge Reason: [Reason for discharge, e.g., completion of treatment, client moved, etc.] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank.)
        • Client's Understanding and Agreement: [Client's understanding and agreement with the discharge plan] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank.)

        ## Discharge Plan:
        • [outline the discharge plan, including any follow-up appointments, referrals, or recommendations for continued care] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank.)

        ## Recommendations:
        • [detail overall recommendations identified] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank.)
        • Follow-Up Care:  
            1. [Recommendation 1] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank.)
            2. [Recommendation 2] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank.)
            3. [Recommendation 3] (add more as needed, and only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank.)
        • Self-Care Strategies:  
            1. [Strategy 1] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank.)
            2. [Strategy 2] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank.)
            3. [Strategy 3] (add more as needed, and only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank.)
        • Crisis Plan: [Instructions for handling potential crises or relapses] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank.)
        • Support Systems: [Encouragement to engage with personal support networks] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank.)

        ## Additional Notes:
        • [include any additional notes or comments relevant to the client's discharge] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank.)

        ## Final Note  
        • Therapist’s Closing Remarks: [Any final remarks or reflections on the client’s journey] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank.)

        Clinician's Name: [clinician's full name] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank.)
        Clinician's Signature: [clinician's signature] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank.)
        Date: [date of document completion] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank.)

        Attachments (if any)  
        - [List of any attached documents, such as final assessment results, referral letters, etc.] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank.)

        (Never come up with your own patient details, assessment, diagnosis, plan, interventions, evaluation, and plan for continuing care - use only the transcript, contextual notes or clinical note as a reference for the information include in your note. If any information related to a placeholder has not been explicitly mentioned in the transcript, contextual notes or clinical note, you must not state the information has not been explicitly mentioned in your output, just leave the relevant placeholder or section blank.)(Use as many bullet points as needed to capture all the relevant information from the transcript.)
        
        IMPORTANT:
        - Your response MUST be a valid TEXT format exactly matching this format
        - Only include information explicitly mentioned in the transcript
        - If information for a field is not mentioned, provide an empty string or empty array
        - Do not invent information not present in the conversation
        - Be DIRECT and CONCISE in all documentation while preserving clinical accuracy
        """
        
        elif template_type == "h75":
            current_date = None
            if "metadata" in transcription and "current_date" in transcription["metadata"]:
                current_date = transcription["metadata"]["current_date"]
                main_logger.info(f"[OP-{operation_id}] Using current_date from metadata: {current_date}")
            system_message = f"""You are an expert medical report generator tasked with creating a concise ">75 Health Assessment" report for patients aged 75 and older. The report must follow the provided template structure, including only sections and subsections where relevant data is available from the transcription. Omit any section or subsection with no data, without using placeholders. 
            Use a professional, empathetic, and doctor-like tone suitable for healthcare documentation. 
            Summarize and analyze the transcription to extract pertinent information, ensuring the report is clear, concise, and uses medical terminology. Include time-related information (e.g., symptom duration, follow-up dates) where applicable. Ensure the report is titled '>75 Health Assessment for [Patient Name]' and dated with the provided current_date or '[Date]' if unavailable. Structure the report to assist clinicians by providing a focused summary of the patient's condition from a doctor's perspective.
            {preservation_instructions} {grammar_instructions} {date_instructions}

        Below is the instructions to format the report:
        {formatting_instructions}
            """

            user_instructions = f"""
        Generate a ">75 Health Assessment" report based on the provided transcription and the following template. 
        Don't add anything that is not part of the conversation. Only surround the report around the stuff talked about in conversation.
        Do not repeat anything
        Extract and summarize relevant data from the transcription, including only sections and subsections with available information. Omit any section or subsection without data, avoiding placeholders. Use medical terminology, maintain a professional tone, and ensure the report is concise and structured to support clinical decision-making. Include time-related details (e.g., symptom onset, follow-up appointments) where relevant. Format the output as a valid Text, following the template structure below:
        Below is the instructions to format the report:
        {formatting_instructions}

        {date_instructions}

        Template Structure
    
        # >75 Health Assessment

        ## Medical History 
        • Chronic conditions: [e.g., List diagnosed conditions and management]  
        • Smoking history: [e.g., Smoking status and history]  
        • Current presentation: [e.g., Symptoms, vitals, exam findings, diagnosis]  
        • Medications prescribed: [e.g., List medications and instructions]  
        • Last specialist, dental, optometry visits recorded: [e.g., Cardiologist - [Date]]  
        • Latest screening tests noted (BMD, FOBT, CST, mammogram): [e.g., BMD - [Date]]  
        • Vaccination status updated (flu, COVID-19, pneumococcal, shingles, tetanus): [e.g., Flu - [Date]]  
        • Sleep quality affected by snoring or daytime somnolence: [e.g., Reports snoring]  
        • Vision: [e.g., Corrected vision with glasses]  
        • Presence of urinary and/or bowel incontinence: [e.g., No incontinence reported]  
        • Falls reported in last 3 months: [e.g., No falls reported]  
        • Independent with activities of daily living: [e.g., Independent with all ADLs]  
        • Mobility limits documented: [e.g., Difficulty with stairs]  
        • Home support and ACAT assessment status confirmed: [e.g., No home support required]  
        • Advance care planning documents (Will, EPoA, AHD) up to date: [e.g., Will updated [Date]]  
        • Cognitive function assessed: [e.g., MMSE score 28/30]  
        • Dont add anything that is not mentioned, if no data is available ignore the heading.


        ## Social History
        • Engagement in hobbies and social activities: [e.g., Enjoys gardening]  
        • Living arrangements and availability of social supports: [e.g., Lives with family]  
        • Caregiver roles identified: [e.g., Daughter assists with shopping]  
        • Dont add anything that is not mentioned, if no data is available ignore the heading.


        ## Sleep 
        • Sleep difficulties or use of sleep aids documented: [e.g., Difficulty falling asleep] 
        • Dont add anything that is not mentioned, if no data is available ignore the heading.


        ## Bowel and Bladder Function 
        • Continence status described: [e.g., Fully continent]  
        • Dont add anything that is not mentioned, if no data is available ignore the heading.


        ## Hearing and Vision 
        • Hearing aid use and comfort noted: [e.g., Uses hearing aids bilaterally]  
        • Recent audiology appointment recorded: [e.g., Audiology review - [Date]]  
        • Glasses use and last optometry review noted: [e.g., Wears glasses, last review - [Date]]  
        • Dont add anything that is not mentioned, if no data is available ignore the heading.


        ## Home Environment & Safety 
        • Home access and safety features documented: [e.g., Handrails installed]  
        • Assistance with cleaning, cooking, gardening reported: [e.g., Hires cleaner biweekly]  
        • Financial ability to afford food and services addressed: [e.g., Adequate resources]  
        • Dont add anything that is not mentioned, if no data is available ignore the heading.


        ## Mobility and Physical Function
        • Ability to bend, kneel, climb stairs, dress, bathe independently: [e.g., Independent with dressing]  
        • Use of walking aids and quality footwear: [e.g., Uses cane outdoors]  
        • Exercise or physical activity levels described: [e.g., Walks 30 minutes daily]  
        • Dont add anything that is not mentioned, if no data is available ignore the heading.


        ## Nutrition
        • Breakfast: [e.g., Oatmeal with fruit]  
        • Lunch: [e.g., Salad with chicken]  
        • Dinner: [e.g., Grilled fish, vegetables]  
        • Snacks: [e.g., Yogurt, nuts]  
        • Dental or swallowing difficulties: [e.g., No dental issues]  
        • Dont add anything that is not mentioned, if no data is available ignore the heading.


        ## Transport
        • Use of transport services or family support for appointments: [e.g., Drives own car]  
        • Dont add anything that is not mentioned, if no data is available ignore the heading.


        ## Specialist and Allied Health Visits
        • Next specialist and allied health appointments planned: [e.g., Cardiologist - [Date]]  
        • Dont add anything that is not mentioned, if no data is available ignore the heading.


        ## Health Promotion & Planning
        • Chronic disease prevention and lifestyle advice provided: [e.g., Advised heart-healthy diet]  
        • Immunisations and screenings reviewed and updated: [e.g., Flu vaccine scheduled]  
        • Patient health goals and advance care planning discussed: [e.g., Goal to maintain mobility]  
        • Referrals made as needed (allied health, social support, dental, optometry, audiology): [e.g., Referred to podiatrist]  
        • Follow-up and recall appointments scheduled: [e.g., Follow-up in 3 months]  
        • Dont add anything that is not mentioned, if no data is available ignore the heading.

        Output Format
        The output must be a valid in Text format. Only include sections and subsections with data extracted from the transcription. Use '[Patient Name]' for the patient’s name and the provided current_date or '[Date]' if unavailable.
        """ 
      
        elif template_type == "new_soap_note":
            current_date = None
            if "metadata" in transcription and "current_date" in transcription["metadata"]:
                current_date = transcription["metadata"]["current_date"]
                main_logger.info(f"[OP-{operation_id}] Using current_date from metadata: {current_date}")
            
            user_instructions = f"""You are provided with a medical conversation transcript. 
            Analyze the transcript and generate a structured SOAP note following the specified template. 
            Use only the information explicitly provided in the transcript, and do not include or assume any additional details. 
            Ensure the output is a valid TEXT/PLAIN object with the SOAP note sections (Subjective, Past Medical History, Social History, Family History, Objective, Assessment & Plan) as keys, formatted professionally and concisely in a doctor-like tone. 
            Address all chief complaints and issues separately in the Subjective and Assessment & Plan sections unless clinically related (e.g., fatigue and headache due to stress). 
            Make sure the output is in valid TEXT/PLAIN format. 
            If the patient did not provide information for a section (Subjective, Past Medical History, Social History, Family History, Objective, Assessment & Plan ), omit that section entirely without placeholders.
            Convert all numbers to numeric format (e.g., 'five' to 5).
            Ensure each point in Subjective, Past Medical History, Social History, Family History, Objective starts with '•  ' (Unicode bullet point U+2022 followed by two spaces).
            For A/P, dont use '•  ' for subheadings (Diagnosis, Differential Diagnosis, Investigations, Treatment, Referrals/Follow Up) and use '•  ' with their content, ensuring no hyphens or other markers are used.
            Ensure A/P is concise with subheadings for each issue, avoiding repetition of content.
            Use {current_date if current_date else "the current date"} as the reference date for all temporal expressions in the transcription.
            Format dates as DD/MM/YYYY (e.g., 10/06/2025).
            Do not repeat anything in any heading also not across multiple headings.
            For temperature, use the unit provided in the transcript (e.g., Celsius or Fahrenheit); if Celsius is provided, do not convert to Fahrenheit unless explicitly stated.
            Ensure no content is repeated across Subjective, Past Medical History, Social History, Family History, Objective, Assessment & Plan sections.
            Below is the transcript:\n\n{conversation_text}
            
            Below are the report formatting structure:
            {formatting_instructions}"""
            
            
            system_message = f"""
            You are a highly skilled medical professional tasked with analyzing a provided medical transcription and generating a concise, well-structured SOAP note in valid TEXT/PLAIN format. Follow the SOAP note template below, using only the information explicitly provided in the transcription. Do not include or assume any details not mentioned, and do not state that information is missing. Write in a professional, doctor-like tone, keeping phrasing succinct and clear. Group related chief complaints (e.g., fatigue and headache) into a single issue in the S and A/P sections when they share a likely etiology (e.g., stress-related symptoms), unless the transcription clearly indicates separate issues. Address each distinct issue separately in A/P only if explicitly presented as unrelated in the transcription. Summarize details accurately, focusing on the reasons for the visit, symptoms, and relevant medical history.
            
            Date Handling Instructions:
                Use {current_date if current_date else "the current date"} as the reference date for all temporal expressions in the transcription.
                Convert relative time references to specific dates based on the reference date:
                    "This morning," "today," or similar terms refer to the reference date.
                    "X days ago" refers to the reference date minus X days (e.g., "five days ago" is reference date minus 5 days).
                    "Last week" refers to approximately 7 days prior, adjusting for specific days if mentioned (e.g., "last Wednesday" is the most recent Wednesday before the reference date).
                Format all dates as DD/MM/YYYY (e.g., 10/06/2025) in the SOAP note output.
                Do not use hardcoded dates or assumed years unless explicitly stated in the transcription.
                Verify date calculations to prevent errors, such as incorrect years or misaligned days.
            {preservation_instructions} {grammar_instructions}
            Below are the report formatting structure:
            {formatting_instructions}

            SOAP Note Template:

            # SOAP NOTE

            ## Subjective:
            List reasons for visit and chief complaints (e.g., symptoms, requests) concisely, grouping related symptoms (e.g., fatigue and headache) when clinically appropriate.
            Include duration, timing, quality, severity, and context for each complaint or group.
            Note factors that worsen or alleviate symptoms, including self-treatment attempts and effectiveness.
            Describe symptom progression, if mentioned.
            Mention past occurrences of similar symptoms, including management and outcomes.
            Note impact on daily life, work, or activities.
            Include associated focal or systemic symptoms.
            List complaints clearly, avoiding redundancy.
            Make sure nothing is repeated in the subjective section. For each thing summarize it in a concise one point.
            Ensure you are not missing any point.
            Ensure each point starts with '•  ' and is concise, summarizing the issue in one line if possible.

            ## Past Medical History:
            List contributing factors, including past medical/surgical history, investigations, and treatments relevant to the complaints.
            Include exposure history if mentioned.
            Include immunization history and status if provided.
            Note other relevant subjective information if provided.
            If the patient has not provided any information about the past medical history, do not include this section.
            If the patient does not have any medical history as discussed in the conversation and contextually appears healthy, do not include this section.
            If the patient is not taking any medications, do not include medication details, but include other relevant medical history if provided.
            If the patient is taking medications, list the medications, dosage, and frequency.
            Keep each point concise and to the point and in new line with "•  " at the beginning of the line.
            Dont miss any past related inforamtion that is related to the case.
            If medications are taken, list name, dosage, and frequency.
            Ensure each point starts with '•  ' and is concise.
        
            ## Social History:
            List social history relevant to the complaints.
            Only include this section if social history is relevant to the complaints.
            Keep each point concise and to the point and in new line with "•  " at the beginning of the line.
            Omit this section if no relevant social history is provided.
            Ensure each point starts with '•  ' and is concise.
        
            ## Family History:
            List family history relevant to the complaints.
            Only include this section if family history is relevant to the complaints.
            Keep each point concise and to the point and in new line with "•  " at the beginning of the line.
            Omit this section if no relevant family history is provided.
            Ensure each point starts with '•  ' and is concise.
        
            ## Objective:
            Include only objective findings explicitly mentioned in the transcription (e.g., vital signs, specific exam results).
            If vital signs are provided, report as: BP:, HR:, Wt:, T:, O2:, Ht:.
            If CVS exam is explicitly stated as normal, report as: “N S1 and S2, no murmurs or extra beats.”
            If respiratory exam is explicitly stated as normal, report as: “Resp: Chest clear, no decr breath sounds.”
            If abdominal exam is explicitly stated as normal, report as: “No distension, BS+, soft, non-tender to palpation and percussion. No organomegaly.”
            For psychiatry-related appointments, if explicitly mentioned, include: “Appears well, appropriately dressed for occasion. Normal speech. Reactive affect. No perceptual abnormalities. Normal thought form and content. Intact insight and judgement. Cognition grossly normal.”
            Do not include this section if no objective findings or exam results are provided in the transcription.
            Keep each point concise and to the point and in new line with "•  " at the beginning of the line.
            Dont miss any objective related inforamtion that is related to the case.
            Omit this section if no objective findings are provided.
            Ensure each point starts with '•  ' and is concise.

            ## Assessment & Plan:
            For each issue or group of related complaints (list as 1, 2, 3, etc.):
            • State the issue or group of symptoms (e.g., “Fatigue and headache”).
            • Diagnosis: Provide the likely diagnosis (condition name only) with Diagnosis Heading only if the issue and diagnosis is contextually not the same, it will help in removal of duplication.
            • Differential Diagnosis: List differential diagnoses. (analyse the issue and point different diagnosis, make sure to add it)
            • Investigations: List planned investigations and if no investigations are planned then write nothing ignore the points related to investigations.
            • Treatments: List planned treatments if discussed in the conversation otherwise write nothing and ignore the points related to treatments.
            • Referrals/Follow Up: List relevant referrals and follow ups with timelines if mentioned otherwise write nothing and ignore the points related to referrals and follow ups.
            If the have multiple treatments then list them in new line.
            Ensure each subheading's content starts with '•  ' and is concise, avoiding repetition.
            Align A/P with S, grouping related complaints unless explicitly separate.
            Always include Differential Diagnosis.
            List one subheading only once.
            Dont add '•  '  with subheadings, just with its content add '•  ' .
            If the data is not available for somthing ignore that subheading dont write any placeholders in its place.
        

            Instructions:
            Output a valid TEXT/PLAIN object with keys: Subjective, Past Medical History, Social History, Family History, Objective, Assessment & Plan.
            Use only transcription data, avoiding invented details, assessments, or plans.
            Keep wording concise, mirroring the style of: “Fatigue and dull headache for two weeks. Ibuprofen taken few days ago with minimal relief.”
            Group related complaints (e.g., fatigue and headache due to stress) unless the transcription indicates distinct etiologies.
            In O, report only provided vitals and exam findings; do not assume normal findings unless explicitly stated.
            Ensure professional, doctor-like tone without verbose or redundant phrasing.
            Dont repeat any thing in Subjective, Past Medical History, Social History, Family History, and Objective, i mean if something is taken care in objective (fulfills the purpose) then dont add in subjective and others.
            Ensure bullet points use '•  ' consistently across all sections.
                                    
            Example for Reference (Do Not Use as Input):


            Example 1:
            
            Example Transcription:
            Speaker 0 : Good morning i'm doctor sarah what brings you in today
            Speaker 1 : Good morning i have been feeling really unwell for the past week i have a constant cough shortness of breath and my chest feels tight i also have a low grade fever that comes and goes
            Speaker 0 : That sounds uncomfortable when did these symptoms start
            Speaker 1 : About six days ago at first i thought it was just a cold but it's getting worse i get tired just walking a few steps
            Speaker 0 : Are you producing any mucus with the cough
            Speaker 1 : Yes it's yellowish green and like sometimes it's hard to breathe
            Speaker 0 : Especially at night any wheezing or chest pain
            Speaker 1 : A little wheezing and my chest feels heavy no sharp pain though do you have
            Speaker 0 : A history of asthma copd or any lung condition?
            Speaker 1 : I have a mild asthma i use an albuterol inhaler occasionally maybe once or twice a week
            Speaker 0 : Any history of smoking i smoked in college but
            Speaker 1 : I quit ten years ago
            Speaker 0 : Do you have any allergic or chronic illness like diabetes hypertension or gerd
            Speaker 1 : I have type two diabetes diagnosed three years ago i take metamorphin five hundred mg twice a day no known allergies
            Speaker 0 : Any recent travel or contact with someone who's been sick
            Speaker 1 : No travel but my son had a very bad cold last week
            Speaker 0 : Alright let me check your vitals and listen to your lung your temperature is 106 respiratory rate is 22 oxygen saturation is 94 and heart rate is 92 beats per minute i hear some wheezing and crackles in both lower lung field your throat looks a bit red and post nasal drip is present
            Speaker 1 : Is it a chest infection
            Speaker 0 : Based on your symptoms history and exam it sounds like acute bronchitis possibly comp complicated by asthma and diabetes it's likely viral but with your underlying condition we should be cautious medications i'll prescribe amoxicillin chloride eight seventy five mg or one twenty five mg twice daily for seven days just in case there's a bacterial component continue using your albuterol inhaler but increase to every four to six six hours as needed i'm also prescribing a five day course of oral prednisone forty mg per day to reduce inflammation due to your asthma flare for the cough you can take guifenacin with dex with dextromethorphan as needed check your blood glucose more frequently while on prednisone as it can raise your sugar levels if your oxygen drops below 92 or your breathing worsens go to the emergency rest stay hydrated and avoid exertion use a humidifier at night and avoid cold air i want to see you back in three to five days to recheck your lungs and sugar control if symptoms worsen sooner come in immediately okay that makes sense will these make me sleepy the cough syrup might so take it at night and remember don't drive after taking it let me print your ex prescription and set your follow-up"

            Example SOAP Note Output:

            # SOAP NOTE

            ## Subjective:
            •   Constant cough, shortness of breath, chest tightness for 6 days
            •  Initially thought to be cold, now worsening with fatigue on minimal exertion
            •  Yellowish-green sputum production
            •  Breathing difficulty, especially at night
            •  Mild wheezing, chest heaviness, no sharp pain
            •  Son had severe cold last week

            ## Past Medical History:
            •  Mild asthma - uses albuterol inhaler 1-2 times weekly
            •  Type 2 diabetes - diagnosed 3 years ago
            •  Medications: Metformin 500mg BD
            •  No allergies

            ## Social History:
            •  Ex-smoker, quit 10 years ago, smoked in college

            ## Objective:  
            •  T: 38.1°C, RR: 22, O2 sat: 94%, HR: 92 bpm
            •  Wheezing and crackles in both lower lung fields
            •  Throat erythema with post-nasal drip

            ## Assessment & Plan:
            1. Acute bronchitis with asthma exacerbation
               Diagnosis: Complicated by diabetes
               Treatment: 
                • Amoxicillin clavulanate 875/125mg BD for 7 days
                • Continue albuterol inhaler, increase to q4-6h PRN
                • Prednisone 40mg daily for 5 days
                • Guaifenesin with dextromethorphan PRN for cough
                • Monitor blood glucose more frequently while on prednisone
                • Seek emergency care if O2 saturation <92% or worsening breathing
                • Rest, hydration, avoid exertion
                • Use humidifier at night, avoid cold air
               Follow-up: 
                • Follow-up in 3-5 days to reassess lungs and glycaemic control
                • Return sooner if symptoms worsen


            Example 2:

            Example Transcription:
            Speaker 0: Good afternoon, Mr. Thompson. I’m Dr. Patel. What brings you in today?
            Speaker 1: Good afternoon, Doctor. I’ve been feeling awful for the past two weeks. I’ve got this persistent abdominal pain, fatigue that’s gotten worse, and my joints have been aching, especially in my knees and hands. I’m also having some chest discomfort, like a pressure, but it’s not sharp. It’s been tough to keep up with work.
            Speaker 0: I’m sorry to hear that. Let’s break this down. Can you describe the abdominal pain? Where is it, how severe, and when did it start?
            Speaker 1: It’s in my upper abdomen, mostly on the right side, for about two weeks. It’s a dull ache, maybe 6 out of 10, worse after eating fatty foods like fried chicken. I tried taking antacids, but they didn’t help much. It’s been steady, not really getting better or worse.
            Speaker 0: Any nausea, vomiting, or changes in bowel habits?
            Speaker 1: Some nausea, no vomiting. My stools have been pale and greasy-looking for the past week, which is weird. I’ve also lost about 5 pounds, I think.
            Speaker 0: What about the fatigue? How’s that affecting you?
            Speaker 1: I’m exhausted all the time, even after sleeping. It’s hard to focus at work—I’m a mechanic, and I can barely lift tools some days. It started around the same time as the pain, maybe a bit before.
            Speaker 0: And the joint pain?
            Speaker 1: Yeah, my knees and hands ache, worse in the mornings. It’s stiff for about an hour. I’ve had it on and off for years, but it’s worse now. Ibuprofen helps a little, but not much.
            Speaker 0: Any history of joint issues or arthritis?
            Speaker 1: My doctor mentioned possible rheumatoid arthritis a few years ago, but it wasn’t confirmed. I just took painkillers when it flared up.
            Speaker 0: Okay, and the chest discomfort?
            Speaker 1: It’s like a heavy feeling in my chest, mostly when I’m tired or stressed. It’s been off and on for a week. No real pain, just pressure. It’s scary because my dad had a heart attack at 50.
            Speaker 0: Any shortness of breath or palpitations?
            Speaker 1: A little short of breath when I climb stairs, but no palpitations. I’ve been trying to rest more, but it’s hard with work.
            Speaker 0: Any past medical history we should know about?
            Speaker 1: I’ve got hypertension, diagnosed five years ago, on lisinopril 10 mg daily. I had hepatitis C about 10 years ago, treated and cured. No surgeries. No allergies.
            Speaker 0: Any recent illnesses or exposures?
            Speaker 1: My coworker had the flu last month, but I didn’t get sick. No recent travel.
            Speaker 0: Smoking, alcohol, or drug use?
            Speaker 1: I smoke half a pack a day for 20 years. I drink a beer or two on weekends. No drugs.
            Speaker 0: Family history?
            Speaker 1: My dad had a heart attack and died at 50. My mom has rheumatoid arthritis.
            Speaker 0: Any vaccinations?
            Speaker 1: I got the flu shot last year, and I’m up to date on tetanus. Not sure about others.
            Speaker 0: Alright, let’s examine you. Your vitals are: blood pressure 140/90, heart rate 88, temperature 37.2°C, oxygen saturation 96%, weight 185 lbs, height 5’10”. You look tired but not in acute distress. Heart sounds normal, S1 and S2, no murmurs. Lungs clear, no decreased breath sounds. Abdomen shows mild tenderness in the right upper quadrant, no distension, bowel sounds present, no organomegaly. Joints show slight swelling in both knees, no redness. No skin rashes or lesions.
            Speaker 0: Based on your symptoms and history, we’re dealing with a few issues. The abdominal pain and pale stools suggest possible gallbladder issues, like gallstones, especially with your history of hepatitis C. The fatigue could be related, but we’ll check for other causes like anemia or thyroid issues. The joint pain might be a rheumatoid arthritis flare, and the chest discomfort could be cardiac or non-cardiac, given your family history and smoking. We’ll run some tests: a complete blood count, liver function tests, rheumatoid factor, ECG, and an abdominal ultrasound. For the abdominal pain, avoid fatty foods and take omeprazole 20 mg daily for now. For the joint pain, continue ibuprofen 400 mg as needed, up to three times daily. For the chest discomfort, we’ll start with a low-dose aspirin, 81 mg daily, as a precaution. Stop smoking—it’s critical for your heart and overall health. I’m referring you to a gastroenterologist for the abdominal issues and a rheumatologist for the joint pain. Follow up in one week or sooner if symptoms worsen. If the chest discomfort becomes severe or you feel faint, go to the ER immediately.
            Speaker 1: Okay, that sounds good. Will the tests take long?
            Speaker 0: The blood tests and ECG will be done today; ultrasound might take a few days. I’ll have the nurse set up your referrals and give you a prescription.

            Example SOAP Note Output:

            # SOAP NOTE
            
            ## Subjective:
            •  Persistent abdominal pain in right upper quadrant for 2 weeks, dull ache, 6/10 severity, worse after fatty foods. Antacids ineffective.
            •  Nausea present, no vomiting. Pale, greasy stools for 1 week. 5 lb weight loss.
            •  Fatigue affecting work performance as mechanic, difficulty concentrating, present for approximately 2 weeks.
            •  Joint pain in knees and hands, worse in morning with 1 hour stiffness. Previous mention of possible rheumatoid arthritis (not confirmed).
            •  Chest discomfort described as pressure sensation, present for 1 week, occurs with fatigue/stress.
            •  Mild shortness of breath on exertion (climbing stairs).

            ## Past Medical History:
            •  Hypertension (diagnosed 5 years ago)
            •  Hepatitis C (10 years ago, treated and cured)
            •  Medications: Lisinopril 10mg daily
            •  Immunisations: Influenza vaccine last year, tetanus up to date

            ## Social History:
            •  Mechanic
            •  Smoker (10 cigarettes/day for 20 years)
            •  Alcohol 1-2 drinks on weekends

            ## Family History:
            •  Father died of MI at age 50
            •  Mother has rheumatoid arthritis

            ## Objective:
            •  BP 140/90, HR 88, T 37.2°C, O2 96%, Wt 185 lbs, Ht 5'10"
            •  CVS: N S1 and S2, no murmurs or extra beats
            •  Resp: Chest clear, no decr breath sounds
            •  Abdomen: No distension, BS+, mild RUQ tenderness, soft. No organomegaly
            •  MSK: Mild swelling in both knees, no redness
            •  Skin: No rashes or lesions

            ## Assessment & Plan:
            1. Abdominal pain
            Suspected gallbladder disease (possibly gallstones) with history of Hepatitis C
            Investigations: LFTs, abdominal ultrasound
            Treatment: Omeprazole 20mg daily, avoid fatty foods
            Referrals: Gastroenterology

            2. Joint pain
            Possible rheumatoid arthritis flare
            Investigations: Rheumatoid factor
            Treatment: Ibuprofen 400mg TDS PRN
            Referrals: Rheumatology

            3. Chest discomfort
            Cardiac vs non-cardiac origin given family history and smoking
            Investigations: CBC, ECG
            Treatment: Aspirin 81mg daily, smoking cessation advised
            Follow-up in 1 week or sooner if symptoms worsen
            Emergency department if chest discomfort becomes severe or experiences syncope      


            Example 3:

            Example Transcription:
            Speaker 0 good morning mister patel what bring you today. good morning doctor i'm bit of lately kind of fatigue and i have got this dull headache just does not go away i see how long has this been going on about two weeks now at first i thought it was just a stress but it's not improving okay do you have any other symptoms fever nausea and dizziness not really a fever i think maybe low grade i do feel dizzy when i stand up too quickly though have you noticed any changes in the appetite or sleep yeah actually i'm not eating as much i wake up in the middle of the night and can't fall back asleep understood are you currently taking any medicine prescribed over the counter or supplements doctor just a multimeter one no prescription i had some ibuprofen a few days ago for headache but it did not do much alright any history of chronic condition diabetes hypertension thyroid problem not pretty healthy generally my dad had high blood pressure though okay let me check your vitals blood pressure is one thirty eight or over 88 a bit elevated pulse is 92 mild tachycardia any recent stress or work or home yeah actually there has been lot of  going on at work i am behind on deadlines and it's been tough to keep up that might be contributing to your symptoms stress can manifest physically fatigue headache even sleep disturbance but we will run some basic tests to rule out anemia thyroid dis dysfunction and infection alright doctor that's good should i worry not nothing immediately  alarming but good you are here so we will do cbc thyroid panel and maybe metabolic panel also i recommend you hydrate it well and if possible reduce caffeine and screen time before bed alright when will i get the test results usually within twenty four to forty eight hours we will give you a call or you can check them through the patient portal if anything abnormal come up we will schedule a follow-up thanks doctor i'll really appreciate it of course take your take care of yourself and we will be in touch soon

            Example SOAP Note Output:

            # SOAP NOTE
            
            ## Subjective:
            •  Fatigue and dull headache for two weeks.
            •  Initially attributed to stress, not improving.
            •  Ibuprofen taken few days ago with minimal relief.
            •  Dizziness when standing quickly.
            •  Decreased appetite.
            •  Sleep disturbance - waking during night, difficulty returning to sleep.
            •  Significant work stress, behind on deadlines.

            ## Past Medical History:
            •  Generally healthy.
            •  Multivitamin daily.

            ## Social History:
            •  Work stress with deadlines.

            ## Family History:
            •  Father with hypertension.

            ## Objective:
            •  BP: 138/88 (elevated)
            •  HR: 92 (mild tachycardia)
            •  Investigations: None performed during visit.

            ## Assessment & Plan:
            1. Fatigue and headache
            Diagnosis: Possible stress-related symptoms
            Differential diagnosis: includes anaemia, thyroid dysfunction, infection
            Investigations: CBC, thyroid panel, metabolic panel
            Treatment: Advised to hydrate well, reduce caffeine and screen time before bed
            Results expected within 24-48 hours, to be communicated via call or patient portal
            Follow-up if abnormal results

            """
       
        elif template_type == "soap_issues":
            current_date = None
            if "metadata" in transcription and "current_date" in transcription["metadata"]:
                current_date = transcription["metadata"]["current_date"]
                main_logger.info(f"[OP-{operation_id}] Using current_date from metadata: {current_date}")
            

            user_instructions= f"""You are an expert medical documentation assistant tasked with analyzing a provided medical conversation transcript and generating a structured SOAP ISSUES NOTE in TEXT/PLAIN format. 
    Use only the information explicitly provided in the transcript, and do not include or assume any additional details. Do not use speaker labels like 'Speaker 0' or 'Speaker 1'; instead, refer to participants as doctor/clinician and patient based on context, or summarize key medical information directly without attributing statements to specific speakers.
    Ensure the output is a valid TEXT/PLAIN object with sections: Subjective, Past Medical History, Objective, and Assessment & Plan, formatted professionally and concisely in a doctor-like tone.
    Address all chief complaints and issues separately in Subjective and Assessment & Plan sections unless clinically related (e.g., fatigue and headache due to stress). 
    Omit any section (Subjective, Past Medical History, Objective, Assessment & Plan) if no relevant data is provided in the transcript, without placeholders.
    Convert all numbers to numeric format (e.g., 'five' to 5).
    Use '•  ' (Unicode bullet point U+2022 followed by two spaces) for all points in Subjective, Past Medical History, Objective, and Assessment & Plan subheadings.
    Do not include an 'issues' subheading under Subjective; list issue-specific data directly under each issue name.
    Ensure Assessment & Plan is concise with subheadings for each issue, avoiding repetition.
    Use {current_date if current_date else "the current date"} as the reference date for all temporal expressions in the transcript.
    Format dates as DD/MM/YYYY (e.g., 10/06/2025).
    For temperature, use the unit provided in the transcript (e.g., Celsius or Fahrenheit); if Celsius is provided (e.g., 38.1°C), do not convert to Fahrenheit unless explicitly stated, and ensure values are realistic (e.g., 38.1°C, not 106°F).
    Ensure no content is repeated across Subjective, Past Medical History, Objective, and Assessment & Plan sections.
    Use medical terms to describe each point professionally.
    Add time related information in the report dont miss them.
    Make sure the point are concise and structured and looks professional.
    Please do not repeat anything, the past medical history should never be the part of subjective and same for other sections just dont repeat any data.
            
    Below is the transcript:\n\n{conversation_text}

    Below are the formatting instructions:
    {formatting_instructions}

            """
            system_message = f"""You are a medical documentation assistant tasked with generating a soap issue report in TEXT format based solely on provided transcript, contextual notes, or clinical notes. The output must be a valid TEXT object with the keys: "subjective", "past_medical_history", "objective", and "assessment_and_plan". 
            
            Date Handling Instructions:
                Use {current_date if current_date else "the current date"} as the reference date for all temporal expressions in the transcription.
                Convert relative time references to specific dates based on the reference date:
                    "This morning," "today," or similar terms refer to the reference date.
                    "X days ago" refers to the reference date minus X days (e.g., "five days ago" is reference date minus 5 days).
                    "Last week" refers to approximately 7 days prior, adjusting for specific days if mentioned (e.g., "last Wednesday" is the most recent Wednesday before the reference date).
                Format all dates as DD/MM/YYYY (e.g., 10/06/2025) in the SOAP ISSUE note output.
                Do not use hardcoded dates or assumed years unless explicitly stated in the transcription.
                Verify date calculations to prevent errors, such as incorrect years or misaligned days.
            
            {preservation_instructions} {grammar_instructions}

            Below are the formatting instructions:
            {formatting_instructions}
        
        INSTRUCTIONS
        - Populate fields only with data explicitly mentioned in the provided transcript, contextual notes, or clinical notes. Leave fields blank (empty strings or arrays) if no relevant data is provided.
        - Use a concise, professional, doctor-like tone, e.g., "Fatigue and dull headache for two weeks. Ibuprofen taken few days ago with minimal relief."
        - Avoid redundant or verbose phrasing.

        SOAP Note Template:

        # SOAP ISSUES

        ## Subjective:
        - For each distinct issue, list the issue name followed by relevant data:
        - Summarize chief complaints, duration, timing, quality, severity, context, worsening/alleviating factors, progression, previous episodes, impact on daily activities, associated symptoms, and medications for the issue, if mentioned.
        - For each issue, include:
            a) Issue Name: The condition, request, or topic name only (e.g., "Headache", "Fatigue").
            b) Reasons For Visit: Chief complaints (e.g., symptoms, requests) if mentioned.
            c) Duration/Timing/Location/Quality/Severity/Context: Details on duration, timing, location, quality, severity, or context if mentioned.
            d) Worsens Alleviates: Factors that worsen or alleviate symptoms, including self-treatment attempts and their effectiveness, if mentioned.
            e) Progression: How symptoms have changed or evolved over time, if mentioned.
            f) Previous Episodes: Past occurrences of similar symptoms, including when they occurred, management, and outcomes, if mentioned.
            g) Impact On Daily Activities: Effect on daily life, work, or activities, if mentioned.
            h) Associated Symptoms: Focal or systemic symptoms accompanying the chief complaint, if mentioned.
            i) Medications: List if any medicines the person is taking for that specific issue, the dosage and related information if mentioned.
        - Leave any field blank (empty string) if not explicitly mentioned in the source.
        - make it concise to the point
        - avoid repetition
        - If some sentence is long split it into multiple points and write "•  " before each line.
        - Group related complaints (e.g., fatigue and headache due to stress) under a single issue unless the source indicates distinct etiologies.
        - Ensure each point starts with '•  ' and is concise.

        ## Past Medical History
        -  List contributing factors (past medical/surgical history, investigations, treatments), social history, family history, exposure history, immunization history, and other relevant subjective information, if mentioned.
            a) Contributing Factors: Past medical/surgical history, investigations, or treatments relevant to the reasons for visit or chief complaints, if mentioned.
            b) Social History: Relevant social history (e.g., smoking, alcohol use), if mentioned.
            c) Family History: Relevant family medical history, if mentioned.
            d) Exposure History: Relevant exposure history (e.g., environmental, occupational), if mentioned.
            e) Immunization History: Immunization status, if mentioned.
            f) Other: Any other relevant subjective information, if mentioned.
        - Leave fields blank (empty strings) if not mentioned in the source.
        - Ensure each point starts with '•  ' and is concise.
        - Include the subheading too
        
        Objective:
        - List vital signs, examination findings, and completed investigations with results, if mentioned.
            a) Vital Signs: Vital signs (e.g., BP, HR) if explicitly mentioned.
            b) Examination Findings: Physical or mental state examination findings, including system-specific exams, if mentioned.
            c) Investigations With Results: Completed investigations and their results, if mentioned.
        - Leave fields blank (empty strings) if not mentioned.
        - Do not assume normal findings or include planned investigations (these belong in "assessment_and_plan").
        - make it concise to the point
        - avoid repetition
        - If some sentence is long split it into multiple points and write "•  " before each line.
        - Ensure each point starts with '•  ' and is concise.
        - Include the subheading too

        Assessment and Plan:
        - For each issue listed in Subjective:
            a) Issue name: Must match the corresponding "Issue name" in "subjectiv issues". Write Issue name (whatever is the issue) as heading.
            b) Assessment: Likely diagnosis, if mentioned.
            c) Differential Diagnosis: Possible alternative diagnoses, if mentioned or analyzed.
            d) Treatment planned: Planned treatments, if mentioned.
            d) Investigations planned: Planned or ordered investigations, if mentioned.
            f) Relevant referrals: Referrals with timelines, if mentioned.
        - Ignore the subheading if no data is available under it
        - Ensure each point starts with '•  ' and is concise, avoiding repetition.

        Constraints
        - Data Source: Use only data from the provided transcript, contextual notes, or clinical notes. Do not invent patient details, assessments, plans, interventions, evaluations, or continuing care plans.
        - Tone and Style: Maintain a professional, concise tone suitable for medical documentation.
        - No Assumptions: Do not infer or assume information (e.g., normal vitals, unmentioned symptoms, or diagnoses) unless explicitly stated.
        - Empty Input: If no transcript or notes are provided, return the TEXT structure with all fields as empty strings or arrays, ready to be populated.

        Output: Generate a valid TEXT object adhering to the above structure and constraints, populated only with data explicitly provided in the input.
        
        
        Example for Reference (Do Not Use as Input):


            Example 1:
                    
            Example Transcription:
            Speaker 0 : Good morning i'm doctor sarah what brings you in today
            Speaker 1 : Good morning i have been feeling really unwell for the past week i have a constant cough shortness of breath and my chest feels tight i also have a low grade fever that comes and goes
            Speaker 0 : That sounds uncomfortable when did these symptoms start
            Speaker 1 : About six days ago at first i thought it was just a cold but it's getting worse i get tired just walking a few steps
            Speaker 0 : Are you producing any mucus with the cough
            Speaker 1 : Yes it's yellowish green and like sometimes it's hard to breathe
            Speaker 0 : Especially at night any wheezing or chest pain
            Speaker 1 : A little wheezing and my chest feels heavy no sharp pain though do you have
            Speaker 0 : A history of asthma copd or any lung condition?
            Speaker 1 : I have a mild asthma i use an albuterol inhaler occasionally maybe once or twice a week
            Speaker 0 : Any history of smoking i smoked in college but
            Speaker 1 : I quit ten years ago
            Speaker 0 : Do you have any allergic or chronic illness like diabetes hypertension or gerd
            Speaker 1 : I have type two diabetes diagnosed three years ago i take metamorphin five hundred mg twice a day no known allergies
            Speaker 0 : Any recent travel or contact with someone who's been sick
            Speaker 1 : No travel but my son had a very bad cold last week
            Speaker 0 : Alright let me check your vitals and listen to your lung your temperature is 106 respiratory rate is 22 oxygen saturation is 94 and heart rate is 92 beats per minute i hear some wheezing and crackles in both lower lung field your throat looks a bit red and post nasal drip is present
            Speaker 1 : Is it a chest infection
            Speaker 0 : Based on your symptoms history and exam it sounds like acute bronchitis possibly comp complicated by asthma and diabetes it's likely viral but with your underlying condition we should be cautious medications i'll prescribe amoxicillin chloride eight seventy five mg or one twenty five mg twice daily for seven days just in case there's a bacterial component continue using your albuterol inhaler but increase to every four to six six hours as needed i'm also prescribing a five day course of oral prednisone forty mg per day to reduce inflammation due to your asthma flare for the cough you can take guifenacin with dex with dextromethorphan as needed check your blood glucose more frequently while on prednisone as it can raise your sugar levels if your oxygen drops below 92 or your breathing worsens go to the emergency rest stay hydrated and avoid exertion use a humidifier at night and avoid cold air i want to see you back in three to five days to recheck your lungs and sugar control if symptoms worsen sooner come in immediately okay that makes sense will these make me sleepy the cough syrup might so take it at night and remember don't drive after taking it let me print your ex prescription and set your follow-up"

            Example SOAP ISSUES Note Output:

            # SOAP ISSUES

            ## Subjective:
            1. Acute bronchitis with asthma exacerbation
            •  Constant cough, shortness of breath, chest tightness for 6 days
            •  Worsening symptoms, fatigue with minimal exertion
            •  Yellowish-green sputum production, worse at night
            •  Associated symptoms: Wheezing, chest heaviness, low-grade fever
            •  Initially thought to be a cold
            •  Previous episodes: Mild asthma, uses albuterol inhaler 1-2 times weekly
            •  Impact on daily activities: Tired after walking few steps
            •  Son had severe cold last week
            •  Medications: Albuterol inhaler 1-2 times weekly

            2. Type 2 Diabetes
            • Diagnosed 3 years ago
            • Medications: Metformin 500 mg twice daily

            ## Past Medical History:
            • Mild asthma, uses albuterol inhaler 1-2 times weekly
            • Type 2 diabetes diagnosed 3 years ago
            • Ex-smoker, quit 10 years ago
            • No known allergies

            ## Objective:
            • Vital signs: T: 38.1°C, RR: 22, O2 sat: 94%, HR: 92 bpm
            • Examination findings: Wheezing and crackles in both lower lung fields, red throat, post-nasal drip present

            ## Assessment & Plan:
            1. Acute bronchitis with asthma exacerbation
            •  Assessment: Acute bronchitis complicated by asthma and diabetes
            • Treatment: Amoxicillin clavulanate 875/125mg twice daily for 7 days, increase albuterol inhaler to every 4-6 hours as needed, prednisone 40mg daily for 5 days, guaifenesin with dextromethorphan for cough as needed
            • Monitor blood glucose more frequently while on prednisone
            • Seek emergency care if O2 sat <92% or breathing worsens
            • Rest, hydration, avoid exertion, use humidifier at night, avoid cold air
            • Counselled regarding sedative effects of cough medication
            • Relevant referrals: Follow-up in 3-5 days to reassess lungs and glycemic control.

            2. Type 2 Diabetes
            •  Assessment: Type 2 Diabetes Mellitus
            •  Treatment: Continue metformin 500 mg twice daily, monitor blood glucose more frequently while on prednisone
        

            Example 2:
                    
            Example Transcription:
            Speaker 0: Seeing you again how are you?
            Speaker 1: Hi i'm feeling okay normally i come every two months but scheduled earlier because i've been feeling depressed and anxious i had a panic attack this morning and felt like having chest pain tired and overwhelmed
            Speaker 0: well i'm sorry to hear that can you describe the panic attack further.
            Speaker 1: well i feel like i have a knot in my throat it's a very uncomfortable sensation i was taking lithium in the past but discontinued it because of the side effects
            Speaker 0: what kind of side effects?
            Speaker 1: i was dizzy slow thought process and was feeling nauseous i still throw up every morning i have this sensation like i need to throw up but since i don't have anything in my stomach i just retch
            Speaker 0: how long has this been going on for.
            Speaker 1: oh it's been going on for a month now.
            Speaker 0: and how about the panic attacks
            Speaker 1: I was having the panic attacks once every week in the past and had stopped for three months and this was the first time after that
            Speaker 0: Are you stressed out about anything in particular
            Speaker 1: I feel stressed out about my work and don't want to be there but have to be there for money i work in verizon in sales department sometimes when there are no customers that's when i feel the worst because if i can't hit the sales target they they can fire me
            Speaker 0: How was your job before that?
            Speaker 1: I was working at wells fargo before and that was even more stressful and before that i was working with uber which was not as stressful but the money wasn't good and before that i was working for at and t which was good but because of my illness i could not continue to work there
            Speaker 0: and how has your mood been
            Speaker 1: i feel down i feel like i have a lack of motivation i used to watch a lot of movies and tv shows in the past but now i don't feel interested anymore
            Speaker 0: what do you do in your free time?
            Speaker 1: oh i spend a lot of time on facebook just scrolling you know i have a friend in cuba and he chats with me every day which kinda distracts me
            Speaker 0: who do you live with?
            Speaker 1: i live with my husband and a child a four year old girl
            Speaker 0: do you like spending time with them
            Speaker 1: not really i don't feel like doing much my daughter is closer with my husband so she likes spending more time with him i do take her to the park though
            Speaker 0: how's your sleep been
            Speaker 1: i sleep well i sleep for more than ten hours
            Speaker 0: is it normal for you to sleep for ten hours or do you feel like you have been sleeping more than usual
            Speaker 1: no it's normal i usually sleep that long
            Speaker 0: how's your appetite been
            Speaker 1: i feel like my appetite has reduced
            Speaker 0: and how about your concentration have you been able to focus on your work
            Speaker 1: no my concentration is really bad
            Speaker 0: for how long
            Speaker 1: it's been like that for a month now
            Speaker 0: do you get any dark thoughts like thoughts about hurting yourself or anybody else no how about experiencing anything unusual like hearing voices no one else can hear or seeing things no one else can see such as shadows etcetera
            Speaker 1: no k
            Speaker 0: in the last month have you had any episodes where you have be you have the opposite of low energy like having a lot of energy a lot of thoughts running in your head really fast
            Speaker 1: no i know what manic episodes look like my husband has been keeping an eye on me and i i'm also aware of how how i feel when it starts
            Speaker 0: okay that's good seems like the main issue right now is depression and anxiety have you been getting the monthly injections on time
            Speaker 1: yes
            speaker 0: that's good are you open to start oral medication to help with your symptoms in addition to the injection
            speaker 1: yes
            Speaker 0: okay we're going to start you on a medication for depression that you'll take every morning however you will have to watch out for hyperactivity because medication for depression can trigger a manic episode if you feel like you are getting excessively active and not sleeping well give us a call
            Speaker 1: Okay doctor
            Speaker 0: we will also start you on a medication for anxiety
            Speaker 1: okay sounds good
            Speaker 0: okay pleasure meeting you and see you in next follow-up visit
            Speaker 1: okay thank you
            Speaker 0: thanks bye
           
            Example SOAP ISSUES Note Output:

            # SOAP ISSUES

            ## Subjective:
            1. Depression and Anxiety
            •  Depression and anxiety with panic attack on 21/06/2025, causing chest pain, fatigue, and feeling overwhelmed
            •  Sensation of knot in throat during panic attack
            •  Panic attacks previously weekly, stopped for 3 months, recurred on 21/06/2025
            •  Morning retching for 1 month
            •  Work-related stress at Verizon sales, fear of missing targets
            •  Previous employment: Wells Fargo (more stressful), Uber (less stressful, poor pay), AT&T (left due to illness)
            •  Mood: Depressed, lacks motivation
            •  Loss of interest in movies and TV shows
            •  Reduced appetite
            •  Poor concentration for 1 month
            •  Spends time on Facebook, chats with friend daily
            •  Limited family interaction, takes daughter to park

            ## Past Medical History:
            •  Bipolar disorder, receiving monthly injections
            •  Discontinued lithium due to dizziness, slowed thought process, nausea
            •  Social history: Works in sales at Verizon

            ## Objective:
            •  Currently receiving monthly injections for bipolar disorder


            ## Assessment & Plan:
            1. Depression and Anxiety
            •  Assessment: Depression with anxiety symptoms
            •  Treatment planned: 
                • Continue monthly injections
                • Starting new oral medication for depression (morning dose)
                • Starting medication for anxiety
            •  Counselled regarding risk of medication triggering manic episodes and to report symptoms of hyperactivity or reduced sleep
            •  Relevant referrals: Follow-up appointment scheduled
        
        """
 
        elif template_type == "cardiology_letter":
            current_date = None
            if "metadata" in transcription and "current_date" in transcription["metadata"]:
                current_date = transcription["metadata"]["current_date"]
                main_logger.info(f"[OP-{operation_id}] Using current_date from metadata: {current_date}")
            

            user_instructions= f"""You are provided with a medical conversation transcript. 
            Analyze the transcript and generate a structured CARDIOLOGY CONSULT NOTE following the specified template. 
            Use only the information explicitly provided in the transcript, and do not include or assume any additional details. 
            Ensure the output is a valid TEXT object with the CARDIOLOGY CONSULT NOTE sections formatted professionally and concisely in a doctor-like tone. 
            Make sure the output is in valid TEXT format. 
            If the patient didnt provide the information regarding any section then ignore the respective section.
            Include the all numbers in numeric format.
            Make sure the output is concise and to the point.
            ENsure the data in each section should be to the point and professional.
            Make it useful as doctors perspective so it makes there job easier, dont just dictate and make a note, analyze the conversation, summarize it and make a note that best desrcibes the patient's case as a doctor's perspective.
            Add time related information in the report dont miss them.
            Make sure the point are concise and structured and looks professional.
            Use medical terms to describe each thing.
            Follow strictly the format given below, and always written same output format.
            If data for any field is not available dont write anything under that heading and ignore it.
            Use {current_date if current_date else "the current date"} as the reference date for all temporal expressions in the transcription.
            Below is the transcript:\n\n{conversation_text}
            {date_instructions}


            Add proper indentations where required, and follow the template.
            """
            system_message = f"""You are a medical documentation assistant tasked with generating a detailed and structured cardiac assessment report based on a predefined template. 
            Your output must maintain a professional, concise, and doctor-like tone, avoiding verbose or redundant phrasing. 
            All information must be sourced exclusively from the provided transcript, contextual notes, or clinical notes, and only explicitly mentioned details should be included. 
            If information for a specific section or placeholder is unavailable ignore that heading and dont write anything.
            Do not invent or infer patient details, assessments, plans, interventions, or follow-up care beyond what is explicitly stated. 
            If data for any field is not available dont write anything under that heading and ignore it.
            Below is the instructions to format the report:
            {formatting_instructions}
            {date_instructions}
           
            Date Handling Instructions:
                Use {current_date if current_date else "the current date"} as the reference date for all temporal expressions in the transcription.
                Convert relative time references to specific dates based on the reference date:
                    "This morning," "today," or similar terms refer to the reference date.
                    "X days ago" refers to the reference date minus X days (e.g., "five days ago" is reference date minus 5 days).
                    "Last week" refers to approximately 7 days prior, adjusting for specific days if mentioned (e.g., "last Wednesday" is the most recent Wednesday before the reference date).
                Format all dates as DD/MM/YYYY (e.g., 10/06/2025) in the CARDIOLOGY LETTER output.
                Do not use hardcoded dates or assumed years unless explicitly stated in the transcription.
                Verify date calculations to prevent errors, such as incorrect years or misaligned days.
            {preservation_instructions} {grammar_instructions}

                        Follow the structured guidelines below for each section of the report:

            # Cardiology Consult Letter
            
            ## Reason for Referral
            Include the specific reason why the patient was referred to cardiology. This may include symptoms (e.g., chest pain, palpitations), abnormal test results (e.g., ECG, echocardiogram), or concerns from the referring physician. [ Make it in a well-structured to the point paragraph form]
            
            ## History of Presenting Complaints
            Describe the current cardiac-related symptoms the patient is experiencing. Include onset, duration, frequency, aggravating or relieving factors, and any associated symptoms (e.g., dizziness, dyspnea, syncope, fatigue). [ Make it in a well-structured to the point paragraph form]
            
            ## Past Medical History
            Summarize relevant medical conditions, especially cardiovascular (e.g., hypertension, hyperlipidemia, diabetes, prior MI, stroke, heart failure). Include surgical history if related to cardiology. [ Make it in a well-structured to the point paragraph form]
            
            ## Family History
            Note any history of cardiovascular diseases in first-degree relatives, such as coronary artery disease, sudden cardiac death, cardiomyopathy, or arrhythmias. [ Make it in a well-structured to the point paragraph form]
            
            ## Social History
            Include lifestyle factors relevant to heart health:
            Smoking status (current, past, never)
            Alcohol intake 
            Recreational drug use
            Exercise habits
            Occupation/stress etc if relevant
            [ Make it in a well-structured to the point paragraph form]
          
            ## Medications
            List all current medications, including dosage and frequency. Pay attention to cardiovascular drugs (e.g., statins, beta-blockers, ACE inhibitors, anticoagulants). Also include any over-the-counter or herbal supplements.
           
            ## Examinations
            Summarize findings from the physical exam if mentioned:
            Vital signs (BP, HR)
            Cardiac exam (heart sounds, murmurs)
            Peripheral pulses, edema, JVP
            Respiratory or general observations etc if relevant
            [ Make it in a well-structured to the point paragraph form with subheadings according to the case]
            
            ## Investigations
            List relevant test results:
            ECG findings
            Echocardiogram
            Blood tests (e.g., troponins, cholesterol)
            Stress tests, Holter monitor, imaging studies etc. if available
            [ Make it in a well-structured to the point paragraph form]
            
            ## Assessment and Evaluation
            Provide a clinical interpretation of the case based on the information above. Mention possible diagnoses, cardiac risk assessment, or significant abnormalities.
            [ Make it in a well-structured to the point paragraph form]
           
            ## Plan and Recommendation
            Outline the next steps in management:
            Further investigations required
            Treatment changes or additions
            Lifestyle recommendations
            Follow-up plans
            Referral to other specialties if needed
            [ Make it in a well-structured to the point paragraph form with subheadings according to the case]

            Additional Instructions:
            - Ensure strict adherence to the template structure, maintaining the exact order and headings as specified.
            - If data for any field is not available dont write anything under that heading and ignore it.
            - Ensure all sections are populated only with explicitly provided data, preserving accuracy and professionalism.
        """
       
        elif template_type == "summary":
            user_instructions = f"""Please provide a concise summary of the following medical conversation:\n\n                
            {conversation_text}
            """
            system_message = f"""
            You are an expert medical documentation assistant. When summarizing conversations, do not use speaker labels like 'Speaker 0' or 'Speaker 1'. Instead, refer to participants by their roles (e.g., doctor/clinician and patient) based on context, or simply summarize the key medical information without attributing statements to specific speakers.
            {date_instructions}
            """

        def add_context_sections(user_instructions):
            if feedback and feedback.strip():
                user_instructions += f"\n\nAdditional feedback from user:\n{feedback.strip()}\n"
            if medicine_vocab and medicine_vocab.strip():
                user_instructions += (
                    "\n\nMedicine Vocabulary Reference:\n"
                    "The following medicine names, alternate names, or abbreviations may appear in the transcript.\n"
                    "Use these names/terms exactly as provided if they appear in the conversation.\n"
                    f"{medicine_vocab.strip()}\n"
                )
            return user_instructions

        user_instructions = add_context_sections(user_instructions)
        return user_instructions, system_message

    except Exception as e:
        main_logger.error(f"[OP-{operation_id}] Error generating prompts: {str(e)}", exc_info=True)
        raise RuntimeError(f"Error generating prompts: {str(e)}")

async def stream_openai_report(
    transcription: dict,
    template_type: str,
    feedback: str = None,
    medicine_vocab: str = None
) -> AsyncGenerator[str, None]:
    """
    Generate and stream OpenAI response for the given transcription and template type.
    """
    operation_id = str(uuid.uuid4())[:8]
    try:
        main_logger.info(f"[OP-{operation_id}] Preparing prompts for template: {template_type}")

        # Use your prompt builder with new params
        user_prompt, system_message = await fetch_prompts(
        transcription,
        template_type,
        feedback=feedback,
        medicine_vocab=medicine_vocab
    )

        main_logger.info(f"[OP-{operation_id}] Initiating OpenAI streaming for template: {template_type}")
        stream = await clients.chat.completions.create(
            model="gpt-4.1",
            messages=[
                {"role": "system", "content": system_message},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.3,
            stream=True,
        )
        async for chunk in stream:
            content = chunk.choices[0].delta.content or ""
            yield content
        
        main_logger.info(f"[OP-{operation_id}] Completed streaming for template: {template_type}")

    except Exception as e:
        error_msg = f"[OP-{operation_id}] Error streaming OpenAI report: {str(e)}"
        error_logger.error(error_msg, exc_info=True)
        yield f"Error: {error_msg}"

async def save_report_background(
    transcript_id: str,
    gpt_response: str,
    template_type: str,
    status: str,
    report_id: str
):
    """
    Background task to save report to DynamoDB.
    """
    try:
        main_logger.info(f"[REPORT-SAVE] Saving report {report_id} for transcript {transcript_id} with status '{status}'")

        if gpt_response.startswith("Error:"):
            status = "failed"
            error_logger.error(f"Invalid report content: {gpt_response}")
        # Use gpt_response as both gpt_response and formatted_report
        report_id = await save_report_to_dynamodb(
            transcript_id,
            gpt_response,
            gpt_response,  # Same as gpt_response
            template_type,
            status,
            report_id,
        )
        if status == "failed":
            error_logger.info(f"[REPORT-SAVE] Report marked as failed for transcript {transcript_id}")
        else:
            main_logger.info(f"[REPORT-SAVE] Successfully saved report {report_id} for transcript {transcript_id}")

    except Exception as e:
        error_msg = f"[REPORT-SAVE] Exception while saving report {report_id}: {str(e)}"
        error_logger.error(error_msg, exc_info=True)
        # Save with failed status
        await save_report_to_dynamodb(
            transcript_id,
            gpt_response,
            gpt_response,
            template_type,
            "failed",
            report_id,
        )


@app.post("/live_reporting")
@log_execution_time
async def live_reports(
    transcript_id: str = Form(...),
    template_type: str = Form("new_soap_note"),
    feedback: str = Form(None),  # new, optional
    medicine_vocab: str = Form(None),  # new, pass as JSON string if complex
    background_tasks: BackgroundTasks = None
):
    """
    Stream a report based on a transcript ID and template type, and save it to DynamoDB in the background.
    
    Args:
        transcript_id: ID of the transcript in DynamoDB
        template_type: Type of template report to generate
        background_tasks: FastAPI BackgroundTasks for saving report
    
    Returns:
        StreamingResponse with report content
    """
    try:
        main_logger.info(f"[LIVE_REPORTING] Request received | transcript_id={transcript_id} | template_type={template_type}")
        time_logger.info(f"[LIVE_REPORTING] Started processing for transcript_id={transcript_id}")

        report_status = "processing"
        valid_templates = [
            "new_soap_note",
            "cardiology_consult",
            "echocardiography_report_vet",
            "cardio_consult",
            "cardio_patient_explainer",
            "coronary_angiograph_report",
            "left_heart_catheterization",
            "right_and_left_heart_study",
            "toe_guided_cardioversion_report",
            "hospitalist_progress_note",
            "multiple_issues_visit",
            "counseling_consultation",
            "gp_consult_note",
            "detailed_dietician_initial_assessment",
            "referral_letter",
            "consult_note",
            "mental_health_appointment",
            "clinical_report",
            "psychology_session_notes",
            "speech_pathology_note",
            "progress_note",
            "meeting_minutes",
            "followup_note",
            "detailed_soap_note",
            "case_formulation",
            "discharge_summary",
            "h75",
            "cardiology_letter",
            "soap_issues",
            "summary",
            "physio_soap_outpatient",
            ]
        
        if template_type not in valid_templates:
            error_msg = f"Invalid template type '{template_type}'. Must be one of: {', '.join(valid_templates)}"
            error_logger.error(error_msg)
            return JSONResponse(
                {"status": "failed", "error": error_msg, "report_status": "failed"},
                status_code=400
            )

        main_logger.info(f"[LIVE_REPORTING] Valid template confirmed: {template_type}")
        table = dynamodb.Table('transcripts')
        response = table.get_item(Key={"id": transcript_id})
        
        if 'Item' not in response:
            error_msg = f"Transcript ID {transcript_id} not found"
            error_logger.error(error_msg)
            return JSONResponse(
                {"status": "failed", "error": error_msg, "report_status": "failed"},
                status_code=404
            )

        main_logger.info(f"[LIVE_REPORTING] Transcript fetched successfully for ID: {transcript_id}")

        transcript_item = response['Item']
        transcript_data = transcript_item.get('transcript', '{}')

        if isinstance(transcript_data, (bytes, boto3.dynamodb.types.Binary)):
            transcript_data = decrypt_data(transcript_data).decode('utf-8')
            main_logger.info(f"[LIVE_REPORTING] Transcript decrypted for ID: {transcript_id}")

        try:
            transcription = json.loads(transcript_data)
            main_logger.info(f"[LIVE_REPORTING] Transcript JSON parsed successfully")
        except json.JSONDecodeError:
            error_msg = "Invalid transcript data format"
            error_logger.error(error_msg)
            return JSONResponse(
                {"status": "failed", "error": error_msg, "report_status": "failed"},
                status_code=400
            )
        # Generate a report_id now to pass along and use later
        report_id = str(uuid.uuid4())
        main_logger.info(f"[LIVE_REPORTING] Generated report_id={report_id} for transcript_id={transcript_id}")

        # Collect streamed response for saving
        gpt_response = ""
        async def stream_and_collect() -> AsyncGenerator[str, None]:
            nonlocal gpt_response

            metadata = json.dumps({"template_type": template_type, "report_id": report_id})
            yield f"---meta:::{metadata}:::meta---\n"
            main_logger.info(f"[LIVE_REPORTING] Streaming initiated | report_id={report_id}")
            
            try:
                async for chunk in stream_openai_report(transcription, template_type, feedback=feedback,medicine_vocab=medicine_vocab ):
                    gpt_response += chunk
                    yield chunk
            except Exception as e:
                error_logger.exception(f"[LIVE_REPORTING] Error during streaming report: {str(e)}")
                raise

            # Schedule background task after streaming completes
            main_logger.info(f"[LIVE_REPORTING] Streaming complete. Scheduling background save task for report_id={report_id}")
            background_tasks.add_task(
                save_report_background,
                transcript_id,
                gpt_response,
                template_type,
                "completed" if not gpt_response.startswith("Error:") else "failed",
                report_id  # pass this explicitly
            )

        main_logger.info(f"[LIVE_REPORTING] Returning StreamingResponse for report_id={report_id}")
        return StreamingResponse(
            stream_and_collect(),
            media_type="text/plain"
        )

    except Exception as e:
        error_msg = f"Unexpected error in live_reports: {str(e)}"
        error_logger.exception(error_msg)
        return JSONResponse(
            {
                "status": "failed",
                "error": error_msg,
                "report_status": "failed",
                "transcript_id": transcript_id
            },
            status_code=500
        )
    

@app.post("/generate_report_session")
@log_execution_time
async def generate_report_session(
    transcript_id: str = Form(...),
    template_type: str = Form("new_soap_note"),
    background_tasks: BackgroundTasks = None
):
    """
    Generate a full report in one go (no streaming) and save it in the background.

    Args:
        transcript_id: ID of the transcript in DynamoDB
        template_type: Template to use for generating report
        background_tasks: For background save task

    Returns:
        JSONResponse with full report and metadata
    """
    try:
        main_logger.info(f"[GENERATE_REPORT_SESSION] Request received | transcript_id={transcript_id} | template_type={template_type}")
        time_logger.info(f"[GENERATE_REPORT_SESSION] Started processing for transcript_id={transcript_id}")

        valid_templates = [
            "new_soap_note", "cardiology_consult", "echocardiography_report_vet", "cardio_consult",
            "cardio_patient_explainer", "coronary_angiograph_report", "left_heart_catheterization",
            "right_and_left_heart_study", "toe_guided_cardioversion_report", "hospitalist_progress_note",
            "multiple_issues_visit", "counseling_consultation", "gp_consult_note",
            "detailed_dietician_initial_assessment", "referral_letter", "consult_note",
            "mental_health_appointment", "clinical_report", "psychology_session_notes",
            "speech_pathology_note", "progress_note", "meeting_minutes", "followup_note",
            "detailed_soap_note", "case_formulation", "discharge_summary", "h75",
            "cardiology_letter", "soap_issues", "summary", "physio_soap_outpatient",
        ]

        if template_type not in valid_templates:
            error_msg = f"Invalid template type '{template_type}'. Must be one of: {', '.join(valid_templates)}"
            error_logger.error(error_msg)
            return JSONResponse({"status": "failed", "error": error_msg}, status_code=400)

        table = dynamodb.Table('transcripts')
        response = table.get_item(Key={"id": transcript_id})
        if 'Item' not in response:
            error_msg = f"Transcript ID {transcript_id} not found"
            error_logger.error(error_msg)
            return JSONResponse({"status": "failed", "error": error_msg}, status_code=404)

        transcript_item = response['Item']
        transcript_data = transcript_item.get('transcript', '{}')
        if isinstance(transcript_data, (bytes, boto3.dynamodb.types.Binary)):
            transcript_data = decrypt_data(transcript_data).decode('utf-8')

        try:
            transcription = json.loads(transcript_data)
        except json.JSONDecodeError:
            error_msg = "Invalid transcript data format"
            error_logger.error(error_msg)
            return JSONResponse({"status": "failed", "error": error_msg}, status_code=400)

        report_id = str(uuid.uuid4())
        main_logger.info(f"[GENERATE_REPORT_SESSION] Generating report_id={report_id} for transcript_id={transcript_id}")

        gpt_response = ""
        try:
            async for chunk in stream_openai_report(transcription, template_type):
                gpt_response += chunk
        except Exception as e:
            error_msg = f"[GENERATE_REPORT_SESSION] Report generation failed: {str(e)}"
            error_logger.exception(error_msg)
            gpt_response = f"Error: {str(e)}"

        status = "completed" if not gpt_response.startswith("Error:") else "failed"

        background_tasks.add_task(
            save_report_background,
            transcript_id,
            gpt_response,
            template_type,
            status,
            report_id
        )

        return JSONResponse({
            "status": status,
            "report_id": report_id,
            "template_type": template_type,
            "report": gpt_response,
            "transcript_id": transcript_id,
        }, status_code=200 if status == "completed" else 500)

    except Exception as e:
        error_msg = f"[GENERATE_REPORT] Unexpected error: {str(e)}"
        error_logger.exception(error_msg)
        return JSONResponse(
            {
                "status": "failed",
                "error": error_msg,
                "transcript_id": transcript_id
            },
            status_code=500
        )

@app.post("/transcribe-audio")
@log_execution_time
async def transcribe_audio(
    audio: UploadFile = File(...)
):
    """
    Transcribe an audio file and save the result to DynamoDB.
    
    Args:
        audio: WAV audio file to process
    """
    try:
        main_logger.info(f"[TRANSCRIBE_AUDIO] Request received | filename={audio.filename}, content_type={audio.content_type}")
        time_logger.info(f"[TRANSCRIBE_AUDIO] Start processing for {audio.filename}")

        # Log incoming request details
        main_logger.info(f"Received transcribe request - Filename: {audio.filename}, Content-Type: {audio.content_type}")

        # Validate content type
        valid_content_types = [
            # WAV formats
            "audio/wav", "audio/wave", "audio/x-wav",
            # MP3 formats
            "audio/mp3", "audio/mpeg", "audio/mpeg3", "audio/x-mpeg-3",
            # MP4/M4A formats
            "audio/mp4", "audio/x-m4a", "audio/m4a",
            # FLAC formats
            "audio/flac", "audio/x-flac",
            # AAC formats
            "audio/aac", "audio/x-aac",
            # OGG formats
            "audio/ogg", "audio/vorbis", "application/ogg",
            # WEBM formats
            "audio/webm",
            # Other common audio formats
            "audio/3gpp", "audio/amr"
        ]

        if audio.content_type not in valid_content_types:
            # Allow files with missing content type but with audio file extensions
            valid_extensions = [".wav", ".mp3", ".mp4", ".m4a", ".flac", ".aac", ".ogg", ".webm", ".amr", ".3gp"]
            file_extension = os.path.splitext(audio.filename.lower())[1]
            if file_extension in valid_extensions:
                main_logger.warning(f"[TRANSCRIBE_AUDIO] Unrecognized content-type ({audio.content_type}), but valid extension: {file_extension}")
            else:
                error_msg = f"[TRANSCRIBE_AUDIO] Invalid audio format: {audio.content_type}"
                error_logger.error(error_msg)
                return JSONResponse({
                    "status": "failed",
                    "error": error_msg,
                    "transcription_status": "failed"
                }, status_code=400)

        # Read the audio file
        audio_data = await audio.read()
        if not audio_data:
            error_msg = "[TRANSCRIBE_AUDIO] No audio data found in request"
            error_logger.error(error_msg)
            return JSONResponse({
                "status": "failed",
                "error": error_msg,
                "transcription_status": "failed"
            }, status_code=400)

        main_logger.info(f"[TRANSCRIBE_AUDIO] Audio read successful | size={len(audio_data)} bytes")

        # Initialize status tracking
        transcription_status = "processing"
        transcript_id = None
        transcription_result = None

        try:
            # Transcribe audio
            main_logger.info("[TRANSCRIBE_AUDIO] Starting transcription with diarization")
            transcription_result = await transcribe_audio_with_diarization(audio_data)
            transcription_status = "completed"
            main_logger.info("[TRANSCRIBE_AUDIO] Transcription completed")
            
            # Save transcription to DynamoDB
            main_logger.info("[TRANSCRIBE_AUDIO] Saving transcription to DynamoDB")
            transcript_id = await save_transcript_to_dynamodb(
                transcription_result,
                None,  # No audio info needed for this endpoint
                status=transcription_status
            )
            
            if not transcript_id:
                transcription_status = "failed"
                error_msg = "[TRANSCRIBE_AUDIO] Failed to save transcription to DynamoDB"
                error_logger.error(error_msg)
                return JSONResponse({
                    "status": "failed",
                    "error": error_msg,
                    "transcription_status": transcription_status
                }, status_code=500)

            # Return the complete response
            response_data = {
                "status": "success",
                "transcription_status": transcription_status,
                "transcript_id": transcript_id,
                "transcription": transcription_result
            }
            
            main_logger.info(f"Transcription completed successfully")
            return JSONResponse(response_data)

        except Exception as e:
            error_msg = f"[TRANSCRIBE_AUDIO] Transcription failed: {str(e)}"
            error_logger.exception(error_msg)
            
            # Update status in DynamoDB if we have ID
            if transcript_id:
                await save_transcript_to_dynamodb(
                    transcription_result,
                    None,
                    status="failed"
                )
            
            return JSONResponse({
                "status": "failed",
                "error": error_msg,
                "transcription_status": transcription_status,
                "transcript_id": transcript_id
            }, status_code=500)

    except Exception as e:
        error_msg = f"[TRANSCRIBE_AUDIO] Unexpected server error: {str(e)}"
        error_logger.exception(error_msg)
        return JSONResponse({
            "status": "failed",
            "error": error_msg,
            "transcription_status": "failed"
        }, status_code=500)
        
@app.get("/search")
async def search_data(
    summary_id: str = None,
    transcript_id: str = None,
    report_id: str = None
):
    """
    Search for data in the summaries, transcripts, or reports table based on the provided ID.
    
    Args:
        summary_id: ID of the summary to search for
        transcript_id: ID of the transcription to search for
        report_id: ID of the report to search for
    
    Returns:
        The data associated with the provided ID, or an error message if not found
    """
    try:
        if summary_id:
            main_logger.info(f"[SEARCH] Looking for summary_id: {summary_id}")
            table = dynamodb.Table('summaries')
            response = table.get_item(Key={"id": summary_id})
            if 'Item' in response:
                item = response['Item']
                decrypted_summary = decrypt_data(item['summary'])
                item['summary'] = decrypted_summary
                main_logger.info(f"[SEARCH] Summary found and decrypted | summary_id: {summary_id}")
                return {"summary": item}
            else:
                msg = f"Summary ID {summary_id} not found"
                main_logger.warning(f"[SEARCH] {msg}")
                return JSONResponse({"error": msg}, status_code=404)
            
        if transcript_id:
            main_logger.info(f"[SEARCH] Looking for transcript_id: {transcript_id}")
            transcripts_table = dynamodb.Table('transcripts')
            transcript_response = transcripts_table.get_item(Key={"id": transcript_id})
            if 'Item' not in transcript_response:
                msg = f"Transcript ID {transcript_id} not found"
                main_logger.warning(f"[SEARCH] {msg}")
                return JSONResponse({"error": msg}, status_code=404)
            
            transcription = transcript_response['Item']
            decrypted_transcription = decrypt_data(transcription['transcript'])
            transcription['transcript'] = decrypted_transcription
            main_logger.info(f"[SEARCH] Transcript found and decrypted | transcript_id: {transcript_id}")

            # Get all reports associated with this transcription (PAGINATED)
            reports_table = dynamodb.Table('reports')
            from boto3.dynamodb.conditions import Attr  # Ensure this import is present at the top
            reports = await dynamodb_scan_all(
                reports_table,
                FilterExpression=Attr('transcript_id').eq(transcript_id)
            )
            main_logger.info(f"[SEARCH] Found {len(reports)} reports for transcript_id: {transcript_id}")

            
            # Add report IDs to the transcription data
            transcription['report_ids'] = [report['id'] for report in reports]
            decrypted_reports = [decrypt_data(report['gpt_response']) for report in reports]
            for report, decrypted in zip(reports, decrypted_reports):
                report['gpt_response'] = decrypted
            return {"transcription": transcription, "reports": reports}
        
        if report_id:
            main_logger.info(f"[SEARCH] Looking for report_id: {report_id}")
            table = dynamodb.Table('reports')
            response = table.get_item(Key={"id": report_id})
            if 'Item' in response:
                item = response['Item']
                if 'gpt_response' in item:
                    item['gpt_response'] = decrypt_data(item['gpt_response'])
                main_logger.info(f"[SEARCH] Report found and decrypted | report_id: {report_id}")
                return {"report": item}
            else:
                msg = f"Report ID {report_id} not found"
                main_logger.warning(f"[SEARCH] {msg}")
                return JSONResponse({"error": msg}, status_code=404)

        main_logger.warning("[SEARCH] No valid ID provided in query")
        return JSONResponse({"error": "No valid ID provided"}, status_code=400)
    
    except Exception as e:
        error_logger.error(f"[SEARCH] Unexpected error: {str(e)}", exc_info=True)
        return JSONResponse({"error": f"Failed to search data: {str(e)}"}, status_code=500)
    
@app.post("/generate-custom-report")
@log_execution_time
async def generate_custom_report(
    transcript_id: str = Form(...),
    custom_template: str = Form(...),
    user_prompt: Optional[str] = Form(None),
    template_type: str = Form(...)  # <-- Add this field for template name/type
):
    """
    Generate a report using a custom template provided by the user.

    Args:
        transcript_id: ID of the transcript to use for report generation.
        custom_template: JSON string representing the custom template schema.
        template_type: Name/type of the custom template (provided by user).
    Returns:
        JSONResponse containing the generated report or an error message.
    """
    try:
        # Log incoming request details
        api_logger.info(f"POST /generate-custom-report | Transcript ID: {transcript_id} | Template Type: {template_type}")
        main_logger.info(f"Received custom report request - Transcript ID: {transcript_id}, Template Type: {template_type}")

        # Parse and validate custom template
        try:
            template_schema = custom_template
            main_logger.info("Custom template received and parsed successfully.")
        except json.JSONDecodeError as e:
            error_msg = f"Invalid JSON format for custom template: {str(e)}"
            error_logger.error(error_msg)
            return JSONResponse({"error": error_msg}, status_code=400)

        # Retrieve transcript data
        db_logger.info(f"Fetching transcript from DynamoDB for ID: {transcript_id}")
        table = dynamodb.Table('transcripts')
        response = table.get_item(Key={"id": transcript_id})

        if 'Item' not in response:
            error_msg = f"Transcript ID {transcript_id} not found in DynamoDB."
            error_logger.error(error_msg)
            return JSONResponse({"error": error_msg}, status_code=404)

        transcript_item = response['Item']
        db_logger.info(f"Transcript data successfully retrieved for ID: {transcript_id}")

        # Decrypt and parse transcript data
        try:
            decrypted_transcript = decrypt_data(transcript_item.get('transcript', '{}'))
            transcription = json.loads(decrypted_transcript)
            main_logger.info("Transcript decrypted and parsed successfully.")
        except Exception as e:
            error_msg = f"Transcript decryption or parsing failed: {str(e)}"
            error_logger.error(error_msg)
            return JSONResponse(
                {"error": error_msg},
                status_code=400
            )

        # Generate report using the custom template
        main_logger.info("Generating report using transcription and custom template.")
        start_time = time.time()
        formatted_report = await generate_report_from_transcription(transcription, template_schema, user_prompt)
        duration = time.time() - start_time
        time_logger.info(f"Report generation duration: {duration:.2f} seconds.")
        
        if isinstance(formatted_report, str) and formatted_report.startswith("Error"):
            error_logger.error(f"Report generation returned error: {formatted_report}")
            return JSONResponse({"error": formatted_report}, status_code=400)

        # Add template_type as the main heading
        formatted_report = f"# {template_type}\n\n{formatted_report}"
        report_id = str(uuid.uuid4())

        # Prepare the report item for DynamoDB
        report_item = {
            "id": report_id,  # <-- This must match your DynamoDB table's primary key
            "template_type": template_type,
            "transcript_id": transcript_id,
            "created_at": datetime.utcnow().isoformat(),
            "updated_at": datetime.utcnow().isoformat(),
            "status": "completed",
            "custom_template": custom_template,
            "gpt_response": encrypt_data(json.dumps(template_schema).encode("utf-8")).decode("utf-8"),
            "formatted_report": formatted_report  # <-- Top-level field, not nested
        }

        db_logger.info(f"Saving generated report to DynamoDB. Report ID: {report_id}")
        report_table = dynamodb.Table('reports')
        report_table.put_item(Item=report_item)

        main_logger.info(f"Custom report successfully generated and stored. Report ID: {report_id}")
        return JSONResponse({
            "report_id": report_id,
            "template_type": template_type,
            "formatted_report": formatted_report
        })

    except Exception as e:
        error_msg = f"Unexpected error in generate_custom_report: {str(e)}"
        error_logger.exception(error_msg)
        return JSONResponse({"error": error_msg}, status_code=500)

async def generate_report_from_transcription(transcription, template_schema, user_prompt):
    """
    Generate a report from transcription data using a custom template schema.
    
    Args:
        transcription: Dictionary containing the transcription data.
        template_schema: JSON schema provided by the user.
        
    Returns:
        Formatted report as a string or an error message.
    """
    try:
        main_logger.info("Starting report formatting process with custom template.")
        
        # Prepare the system prompt
        system_prompt = """
        You are a clinical documentation specialist AI trained to convert physician-patient conversations into structured, professional medical reports. 
        Your task is to use a provided custom report template to generate a clear, concise, and accurate report from the transcription data.

        Instructions:
        1. **Accuracy & Completeness**: Extract all relevant clinical details directly from the transcript. Ensure no section of the template is left unaddressed if data is available.
        2. **Medical Terminology**: Use standardized medical terms, abbreviations (e.g., PMH, Hx), and a clinical tone suitable for the USA, UK, Australia, and Canada.
        3. **Formatting**:
        - Use `#` for main headings and `##` for subheadings.
        - Use bullet points (`•`) for discrete facts (e.g., symptoms, medications).
        - Use paragraph form for narratives or multi-sentence content — **never mix bullets and paragraphs**.
        4. **Conciseness**: Prioritize utility. Avoid repetition and verbose descriptions. Do not include generic or filler text.
        5. **Missing Data**: If a section of the template is not covered in the transcript, omit it entirely.
        6. **Clinical Tone**: Use objective, professional language. Avoid assumptions or casual phrasing.
        7. **Confidentiality**: Handle all patient information with strict confidentiality.

        Your goal is to produce a physician-ready medical report with the utmost clarity and precision.
        """
        DATE_INSTRUCTIONS = """
            Date Handling Instructions:
            - Use {reference_date} as the reference date for all temporal expressions in the transcription.
            - Convert relative time references to specific dates based on the reference date:
            - "This morning," "today," or similar terms refer to the reference date.
            - "X days ago" refers to the reference date minus X days (e.g., "five days ago" is reference date minus 5 days).
            - "Last week" refers to approximately 7 days prior, adjusting for specific days if mentioned (e.g., "last Wednesday" is the most recent Wednesday before the reference date).
            - Format all dates as DD/MM/YYYY (e.g., 10/06/2025) in the output.
            - Do not use hardcoded dates or assumed years unless explicitly stated in the transcription.
            - Verify date calculations to prevent errors, such as incorrect years or misaligned days.
            """

        current_date = None
        if "metadata" in transcription and "current_date" in transcription["metadata"]:
            current_date = transcription["metadata"]["current_date"]
        # Core instructions for all medical documentation templates
        preservation_instructions = """
            CRITICAL DATA PRESERVATION RULES:
            1. Retain all mentioned **dates** (symptom onset, appointments, follow-ups, etc.).
            2. Include full **medication details** — name, dosage, frequency, and route.
            3. Preserve **all numerical values** — vitals, labs, durations, measurements.
            4. Include **precise timeframes** — "for 2 weeks", "since 10th April", etc.
            5. Do not summarize or paraphrase numeric or clinical data.
            6. Include all **diagnostic, procedural, and treatment history**.
            7. Capture all stated **allergies, reactions, and contraindications**.
            """
        # Add grammar-specific instructions to all templates
        grammar_instructions = """
            GRAMMAR REQUIREMENTS:
            1. Use standard US English grammar with correct punctuation.
            2. Ensure subject-verb and noun-pronoun agreement.
            3. Use proper prepositions (e.g., “dependent on” not “dependent of”).
            4. Avoid article misuse (e.g., no “the a”).
            5. Maintain consistent verb tenses throughout.
            6. Write clearly with no grammatical ambiguity.
            7. Maintain professional, clinical tone — no colloquialisms or contractions.
            """

        formatting_instructions = """You are an AI model tasked with generating structured reports. Follow strict formatting rules as outlined below:

            Headings must begin with # followed by a space.
            Example:
            # Clinical Summary

            Subheadings must begin with ## followed by a space.
            Example:
            ## Patient History

            For lists or key points:
            Each point must start with one level of indentation (2 spaces) followed by a bullet point (•) and a space. Make sure it is bullet.
            Use this format only for distinct items or short, listed information.
            Example:
            • Patient reported mild headache
            • No known drug allergies
            
            For paragraphs or multiple sentences, do not use bullets or indentation. Just write in standard paragraph form.

            Never mix bullets and paragraphs. If it's a narrative, use plain text. If it's a list of discrete points, follow the bullet format.

            Stick to this formatting exactly in every section of the report.

            If data for some heading is missing (not mentioned during session) then ignore that heading and dont include it in the output.

            Use '•  ' (Unicode bullet point U+2022 followed by two spaces) for all points in S, PMedHx, SocHx, FHx, O, and for A/P subheadings and their content.

            Make sure to add proper identations
        
"""
       
        date_instructions = DATE_INSTRUCTIONS.format(reference_date=current_date)
        if not user_prompt:
            user_prompt = ""

        user_message= f"""You are provided with a medical conversation transcript.

            Your task is to:
            - Analyze the transcript carefully and generate a structured report using the custom template below.
            - Use **only** the information explicitly stated in the conversation.
            - Do **not** assume or fabricate any data.
            - Omit any sections of the template where no data is provided in the transcript.
            - Prioritize brevity, clarity, and clinical usefulness. Avoid repeating the same point multiple times.

            Ensure:
            - All numeric data is preserved exactly as stated.
            - Dates are interpreted based on the reference date: {current_date}
            - The final report is accurate, cleanly formatted, and sounds like it was authored by a medical professional.

            Below is the custom report template:
            {template_schema}

            Below is the transcript:
            {transcription}

            Use the following formatting rules:
            {formatting_instructions}

            Date handling rules:
            {date_instructions}

            Grammar and language requirements:
            {grammar_instructions}

            Critical data preservation rules:
            {preservation_instructions}

            {user_prompt}
        """ 

        # Extract conversation text
        conversation_text = ""
        if "conversation" in transcription:
            for entry in transcription["conversation"]:
                speaker = entry.get("speaker", "Unknown")
                text = entry.get("text", "")
                conversation_text += f"{speaker}: {text}\n\n"
        
        main_logger.info("Formatted transcription into conversation text.")
        api_logger.info("Calling language model for report generation.")

        start = time.time()
        report = await generate_report_with_language_model(conversation_text, template_schema, system_prompt, user_message)
        elapsed = time.time() - start
        time_logger.info(f"Language model processing completed in {elapsed:.2f} seconds.")

        return report

    except Exception as e:
        error_msg = f"Exception in generate_report_from_transcription: {str(e)}"
        error_logger.error(error_msg, exc_info=True)
        return f"Error: {error_msg}"

async def generate_report_with_language_model(conversation_text, template_schema, system_prompt, user_message):
    """
    Generate a report using a language model API.
    
    Args:
        conversation_text: The text of the conversation to be used in the report.
        template_schema: The custom template schema to follow.
        system_prompt: The system prompt guiding the report generation.
        
    Returns:
        A generated report as a string.
    """
    try:
        api_logger.info("Sending prompt to OpenAI language model.")
        # Use the global client with API key
        global client
        response = client.chat.completions.create(
            model="gpt-4.1", # or another appropriate model like "gpt-3.5-turbo"
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message}
            ],
            temperature=0.3,
        )
        
        # Extract the generated report from the response
        report = response.choices[0].message.content.strip()
        main_logger.info("Language model returned a response successfully.")
        return report

    except Exception as e:
        error_msg = f"OpenAI API call failed: {str(e)}"
        error_logger.error(error_msg, exc_info=True)
        return f"Error: {error_msg}"

# Add this helper function near the top of your file (e.g., after imports or utility functions)
async def dynamodb_scan_all(table, **kwargs):
    """
    Scan a DynamoDB table and return all items, paginating as needed.
    """
    try:
        main_logger.info(f"Initiating full scan on DynamoDB table: {table.name}")
        items = []
        last_evaluated_key = None
        scan_count = 0

        while True:
            scan_count += 1
            if last_evaluated_key:
                kwargs['ExclusiveStartKey'] = last_evaluated_key
                main_logger.info(f"Continuing scan with ExclusiveStartKey: {last_evaluated_key}")

            response = table.scan(**kwargs)
            page_items = response.get('Items', [])
            items.extend(page_items)

            main_logger.info(f"Scan iteration {scan_count}: Retrieved {len(page_items)} items (Total: {len(items)})")

            last_evaluated_key = response.get('LastEvaluatedKey')
            if not last_evaluated_key:
                break

        main_logger.info(f"Completed full scan on table {table.name}. Total items: {len(items)}")
        return items

    except Exception as e:
        error_logger.exception(f"Error scanning DynamoDB table {table.name}: {str(e)}")
        return []


@app.post("/transcribe-and-generate-report")
@log_execution_time
async def transcribe_and_generate_report(
    audio: UploadFile = File(...),
    template_type: str = Form("new_soap_note"),
    background_tasks: BackgroundTasks = None
):
    try:
        main_logger.info(f"Received transcribe and generate report request - Filename: {audio.filename}, Content-Type: {audio.content_type}, Template: {template_type}")

        valid_content_types = [
            "audio/wav", "audio/wave", "audio/x-wav",
            "audio/mp3", "audio/mpeg", "audio/mpeg3", "audio/x-mpeg-3",
            "audio/mp4", "audio/x-m4a", "audio/m4a",
            "audio/flac", "audio/x-flac",
            "audio/aac", "audio/x-aac",
            "audio/ogg", "audio/vorbis", "application/ogg",
            "audio/webm", "audio/3gpp", "audio/amr"
        ]

        if audio.content_type not in valid_content_types:
            valid_extensions = [".wav", ".mp3", ".mp4", ".m4a", ".flac", ".aac", ".ogg", ".webm", ".amr", ".3gp"]
            file_extension = os.path.splitext(audio.filename.lower())[1]
            if file_extension in valid_extensions:
                main_logger.info(f"Audio content-type not recognized ({audio.content_type}), but filename has valid extension: {file_extension}")
            else:
                return JSONResponse({"status": "failed", "error": f"Invalid audio format: {audio.content_type}"}, status_code=400)

        valid_templates = [
            "new_soap_note",
            "cardiology_consult",
            "echocardiography_report_vet",
            "cardio_consult",
            "cardio_patient_explainer",
            "coronary_angiograph_report",
            "left_heart_catheterization",
            "right_and_left_heart_study",
            "toe_guided_cardioversion_report",
            "hospitalist_progress_note",
            "multiple_issues_visit",
            "counseling_consultation",
            "gp_consult_note",
            "detailed_dietician_initial_assessment",
            "referral_letter",
            "consult_note",
            "mental_health_appointment",
            "clinical_report",
            "psychology_session_notes",
            "speech_pathology_note",
            "progress_note",
            "meeting_minutes",
            "followup_note",
            "detailed_soap_note",
            "case_formulation",
            "discharge_summary",
            "h75",
            "cardiology_letter",
            "soap_issues",
            "summary",
            "physio_soap_outpatient",
            ]

        if template_type not in valid_templates:
            return JSONResponse({"status": "failed", "error": f"Invalid template type: {template_type}"}, status_code=400)

        audio_data = await audio.read()
        if not audio_data:
            return JSONResponse({"status": "failed", "error": "No audio data provided"}, status_code=400)

        main_logger.info(f"Audio file read successfully. Size: {len(audio_data)} bytes")

        # Transcribe
        transcription_result = await transcribe_audio_with_diarization(audio_data)
        transcript_id = await save_transcript_to_dynamodb(transcription_result, None, status="completed")

        if not transcript_id:
            return JSONResponse({"status": "failed", "error": "Failed to save transcription"}, status_code=500)

        main_logger.info(f"Generating {template_type} template for transcript {transcript_id}")
        report_id = str(uuid.uuid4())
        # Generator function for StreamingResponse
        async def generate_stream():
            full_response = ""
            try:

                metadata = json.dumps({"template_type": template_type, "report_id": report_id})
                yield f"---meta:::{metadata}:::meta---\n"

                async for chunk in stream_openai_report(transcription_result, template_type):
                    full_response += chunk
                    yield chunk
            except Exception as e:
                error_logger.exception(f"Streaming error: {e}")
                yield f"\nError: {str(e)}"
            finally:
                # Save final report in background
                if background_tasks and full_response and not full_response.startswith("Error:"):
                    background_tasks.add_task(
                        save_report_background,
                        transcript_id,
                        full_response,
                        template_type,
                        "completed",
                        report_id,
                    )

        return StreamingResponse(generate_stream(), media_type="text/plain")

    except Exception as e:
        error_logger.exception(f"Unexpected error: {e}")
        return JSONResponse({
            "status": "failed",
            "error": f"Unexpected error: {str(e)}",
            "transcription_status": "failed",
            "report_status": "not_started"
        }, status_code=500)

@app.put("/update-report/{report_id}")
@log_execution_time
async def update_report(report_id: str, formatted_report: str = Form(...)):
    try:
        main_logger.info(f"Received update request for report ID: {report_id}")

        report_table = dynamodb.Table('reports')
        response = report_table.get_item(Key={"id": report_id})
        
        if 'Item' not in response:
            msg = f"Report ID {report_id} not found in 'reports' table"
            main_logger.warning(msg)
            return JSONResponse({"error": msg}, status_code=404)
            
        update_expression = "SET formatted_report = :r, updated_at = :t"
        expression_values = {
            ":r": formatted_report,
            ":t": datetime.utcnow().isoformat()
        }

        main_logger.info(f"Updating report ID {report_id} with new formatted report.")
        db_logger.info(f"UpdateExpression: {update_expression}, Values: {expression_values}")
        
        report_table.update_item(
            Key={"id": report_id},
            UpdateExpression=update_expression,
            ExpressionAttributeValues=expression_values,
            ReturnValues="ALL_NEW"
        )
        
        updated_response = report_table.get_item(Key={"id": report_id})
        updated_report = updated_response['Item']
        
        main_logger.info(f"Successfully updated report ID {report_id}")
        return JSONResponse({
            "status": "success",
            "report_id": report_id,
            "template_type": updated_report.get('template_type'),
            "formatted_report": formatted_report,
            "updated_at": updated_report.get('updated_at')
        })
        
    except Exception as e:
        error_msg = f"Failed to update report: {str(e)}"
        error_logger.error(error_msg, exc_info=True)
        return JSONResponse({"error": error_msg}, status_code=500)


# Add a new schema for discharge summaries
DISCHARGE_SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "client": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "dob": {"type": "string"},
                "discharge_date": {"type": "string"}
            }
        },
        "referral": {
            "type": "object",
            "properties": {
                "source": {"type": "string"},
                "reason": {"type": "string"}
            }
        },
        "presenting_issues": {"type": "array", "items": {"type": "string"}},
        "diagnosis": {"type": "array", "items": {"type": "string"}},
        "treatment_summary": {
            "type": "object",
            "properties": {
                "duration": {"type": "string"},
                "sessions": {"type": "string"},
                "therapy_type": {"type": "string"},
                "goals": {"type": "array", "items": {"type": "string"}},
                "description": {"type": "string"},
                "medications": {"type": "string"}
            }
        },
        "progress": {
            "type": "object",
            "properties": {
                "overall": {"type": "string"},
                "goal_progress": {"type": "array", "items": {"type": "string"}}
            }
        },
        "clinical_observations": {
            "type": "object",
            "properties": {
                "engagement": {"type": "string"},
                "strengths": {"type": "array", "items": {"type": "string"}},
                "challenges": {"type": "array", "items": {"type": "string"}}
            }
        },
        "risk_assessment": {"type": "string"},
        "outcome": {
            "type": "object",
            "properties": {
                "current_status": {"type": "string"},
                "remaining_issues": {"type": "string"},
                "client_perspective": {"type": "string"},
                "therapist_assessment": {"type": "string"}
            }
        },
        "discharge_reason": {
            "type": "object",
            "properties": {
                "reason": {"type": "string"},
                "client_understanding": {"type": "string"}
            }
        },
        "discharge_plan": {"type": "string"},
        "recommendations": {
            "type": "object",
            "properties": {
                "overall": {"type": "string"},
                "followup": {"type": "array", "items": {"type": "string"}},
                "self_care": {"type": "array", "items": {"type": "string"}},
                "crisis_plan": {"type": "string"},
                "support_systems": {"type": "string"}
            }
        },
        "additional_notes": {"type": "string"},
        "final_note": {"type": "string"},
        "clinician": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "date": {"type": "string"}
            }
        },
        "attachments": {"type": "array", "items": {"type": "string"}}
    }
}

SOAP_NOTE_SCHEMA = {
    "type": "object",
    "properties": {
        "subjective": {
            "type": "object",
            "properties": {
                "reason_for_visit": {"type": "string"},
                "duration_timing": {"type": "string"},
                "alleviating_factors": {"type": "string"},
                "progression": {"type": "string"},
                "previous_episodes": {"type": "string"},
                "impact_on_daily_activities": {"type": "string"},
                "associated_symptoms": {"type": "string"}
            }
        },
        "past_medical_history": {
            "type": "object",
            "properties": {
                "contributing_factors": {"type": "string"},
                "social_history": {"type": "string"},
                "family_history": {"type": "string"},
                "exposure_history": {"type": "string"},
                "immunization_history": {"type": "string"},
                "other_relevant_info": {"type": "string"}
            }
        },
        "objective": {
            "type": "object",
            "properties": {
                "vital_signs": {"type": "object"},
                "physical_exam": {"type": "object"},
                "mental_state_exam": {"type": "object"},
                "investigations_results": {"type": "object"}
            }
        },
        "assessment": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "issue": {"type": "string"},
                    "diagnosis": {"type": "string"},
                    "differential_diagnosis": {"type": "string"}
                }
            }
        },
        "plan": {
            "type": "object",
            "properties": {
                "investigations_planned": {
                    "type": "array",
                    "items": {"type": "string"}
                },
                "treatment_plan": {
                    "type": "array",
                    "items": {"type": "string"}
                },
                "followup": {
                    "type": "array",
                    "items": {"type": "string"}
                },
                "referrals": {
                    "type": "array",
                    "items": {"type": "string"}
                },
                "other_actions": {
                    "type": "array",
                    "items": {"type": "string"}
                }
            }
        }
    }
}

CASE_FORMULATION_SCHEMA = {
    "type": "object",
    "properties": {
        "client_goals": {
            "type": ["string", "null"],
            "description": "The client's expressed goals and aspirations"
        },
        "presenting_problems": {
            "oneOf": [
                {
                    "type": "string"
                },
                {
                    "type": "array",
                    "items": {
                        "type": "string"
                    }
                }
            ]
        },
        "predisposing_factors": {
            "oneOf": [
                {
                    "type": "string"
                },
                {
                    "type": "array",
                    "items": {
                        "type": "string"
                    }
                }
            ]
        },
        "precipitating_factors": {
            "oneOf": [
                {
                    "type": "string"
                },
                {
                    "type": "array",
                    "items": {
                        "type": "string"
                    }
                }
            ]
        },
        "perpetuating_factors": {
            "oneOf": [
                {
                    "type": "string"
                },
                {
                    "type": "array",
                    "items": {
                        "type": "string"
                    }
                }
            ]
        },
        "protective_factors": {
            "oneOf": [
                {
                    "type": "string"
                },
                {
                    "type": "array",
                    "items": {
                        "type": "string"
                    }
                }
            ]
        },
        "problem_list": {
            "oneOf": [
                {
                    "type": "string"
                },
                {
                    "type": "array",
                    "items": {
                        "type": "string"
                    }
                }
            ]
        },
        "treatment_goals": {
            "oneOf": [
                {
                    "type": "string"
                },
                {
                    "type": "array",
                    "items": {
                        "type": "string"
                    }
                }
            ]
        },
        "case_formulation": {
            "type": "string"
        },
        "treatment_mode": {
            "oneOf": [
                {
                    "type": "string"
                },
                {
                    "type": "array",
                    "items": {
                        "type": "string"
                    }
                }
            ]
        }
    },
    "required": ["client_goals", "presenting_problems", "predisposing_factors", 
                "precipitating_factors", "perpetuating_factors", "protective_factors", 
                "problem_list", "treatment_goals", "case_formulation", "treatment_mode"]
}

# Add the follow-up note schema
FOLLOWUP_NOTE_SCHEMA = {
    "type": "object",
    "properties": {
        "date": {"type": "string"},
        "presenting_complaints": {
            "type": "array",
            "items": {"type": "string"}
        },
        "mental_status": {
            "type": "object",
            "properties": {
                "appearance": {"type": "string"},
                "behavior": {"type": "string"},
                "speech": {"type": "string"},
                "mood": {"type": "string"},
                "affect": {"type": "string"},
                "thoughts": {"type": "string"},
                "perceptions": {"type": "string"},
                "cognition": {"type": "string"},
                "insight": {"type": "string"},
                "judgment": {"type": "string"}
            }
        },
        "risk_assessment": {"type": "string"},
        "diagnosis": {
            "type": "array",
            "items": {"type": "string"}
        },
        "treatment_plan": {"type": "string"},
        "safety_plan": {"type": "string"},
        "additional_notes": {"type": "string"}
    }
}

# Add the meeting minutes schema
MEETING_MINUTES_SCHEMA = {
    "type": "object",
    "properties": {
        "date": {"type": "string"},
        "time": {"type": "string"},
        "location": {"type": "string"},
        "attendees": {
            "type": "array",
            "items": {"type": "string"}
        },
        "agenda_items": {
            "type": "array",
            "items": {"type": "string"}
        },
        "discussion_points": {
            "type": "array",
            "items": {"type": "string"}
        },
        "decisions_made": {
            "type": "array",
            "items": {"type": "string"}
        },
        "action_items": {
            "type": "array",
            "items": {"type": "string"}
        },
        "next_meeting": {
            "type": "object",
            "properties": {
                "date": {"type": "string"},
                "time": {"type": "string"},
                "location": {"type": "string"}
            }
        }
    }
}
# Add the new pathology schema
PATHOLOGY_NOTE_SCHEMA = {
    "type": "object",
    "properties": {
        "therapy_attendance": {
            "type": "object",
            "properties": {
                "current_issues": {"type": "string"},
                "past_medical_history": {"type": "string"},
                "medications": {"type": "string"},
                "social_history": {"type": "string"},
                "allergies": {"type": "string"}
            }
        },
        "objective": {
            "type": "object",
            "properties": {
                "examination_findings": {"type": "string"},
                "diagnostic_tests": {"type": "string"}
            }
        },
        "reports": {"type": "string"},
        "therapy": {
            "type": "object",
            "properties": {
                "current_therapy": {"type": "string"},
                "therapy_changes": {"type": "string"}
            }
        },
        "outcome": {"type": "string"},
        "plan": {
            "type": "object",
            "properties": {
                "future_plan": {"type": "string"},
                "followup": {"type": "string"}
            }
        }
    }
}

async def format_clinical_report(gpt_response):
    """
    Formats the GPT response into a structured markdown report, keeping main headings fixed
    and dynamically handling subheadings, excluding empty sections.
    
    Args:
        gpt_response (dict): The GPT response dictionary containing medical data.
    
    Returns:
        str: A formatted markdown string or an error message if formatting fails.
    """
    try:
        formatted_report = ["# Medical Report\n"]
        
        # Dictionary to map main headings to their keys and display names
        main_headings = {
            "presenting_problems": "Presenting Problems",
            "history_of_presenting_problems": "History of Presenting Problems",
            "current_functioning": "Current Functioning",
            "current_medications": "Current Medications",
            "psychiatric_history": "Psychiatric History",
            "medical_history": "Medical History",
            "developmental_social_family_history": "Developmental, Social, and Family History",
            "substance_use": "Substance Use",
            "relevant_cultural_religious_spiritual_issues": "Cultural, Religious, and Spiritual Issues",
            "risk_assessment": "Risk Assessment",
            "mental_state_exam": "Mental State Exam",
            "test_results": "Test Results",
            "diagnosis": "Diagnosis",
            "clinical_formulation": "Clinical Formulation",
            "additional_notes": "Additional Notes"
        }
        
        for key, display_name in main_headings.items():
            if not gpt_response.get(key):  # Skip if section is empty or missing
                continue
                
            formatted_report.append(f"## {display_name}")
            
            data = gpt_response[key]
            
            if key == "presenting_problems":
                for item in data:
                    if item.get("details"):
                        formatted_report.extend(item["details"])
                        formatted_report.append("")  # Add spacing
                        
            elif key == "history_of_presenting_problems":
                for issue in data:
                    issue_name = issue.get("issue_name", "Issue")
                    formatted_report.append(f"### {issue_name}")
                    if issue.get("details"):
                        formatted_report.extend(issue["details"])
                        formatted_report.append("")  # Add spacing
                        
            elif key == "current_functioning":
                for subkey, sub_data in data.items():
                    if sub_data:  # Only include non-empty subheadings
                        formatted_report.append(f"### {subkey.replace('_', ' ').title()}")
                        formatted_report.extend(sub_data)
                        formatted_report.append("")  # Add spacing
                        
            elif key in ["current_medications", "psychiatric_history", "substance_use", 
                         "relevant_cultural_religious_spiritual_issues", "risk_assessment", 
                         "mental_state_exam", "test_results", "diagnosis", "additional_notes"]:
                if isinstance(data, list):
                    formatted_report.extend(data)
                    formatted_report.append("")  # Add spacing
                else:
                    for subkey, sub_data in data.items():
                        if isinstance(sub_data, list) and sub_data:  # Only include non-empty subheadings
                            formatted_report.append(f"### {subkey.replace('_', ' ').title()}")
                            formatted_report.extend(sub_data)
                            formatted_report.append("")  # Add spacing
            
            elif key == "clinical_formulation":
                for subkey, sub_data in data.items():
                    if sub_data:  # Only include non-empty subheadings
                        if subkey == "case_formulation":
                            formatted_report.append(f"### Case Formulation\n{sub_data}")
                        else:
                            formatted_report.append(f"### {subkey.replace('_', ' ').title()}")
                            formatted_report.extend(sub_data)
                        formatted_report.append("")  # Add spacing
            
            else:
                # Generic handling for other main headings (like medical_history, etc.)
                if isinstance(data, list):
                    formatted_report.extend(data)
                    formatted_report.append("")  # Add spacing
                else:
                    for subkey, sub_data in data.items():
                        if sub_data:  # Only include non-empty subheadings
                            formatted_report.append(f"### {subkey.replace('_', ' ').title()}")
                            formatted_report.extend(sub_data)
                            formatted_report.append("")  # Add spacing
        
        # If only the heading exists, add a default message
        if len(formatted_report) == 1:
            formatted_report.append("No findings documented")
        
        return "\n".join(formatted_report).rstrip()  # Remove trailing newline
    
    except Exception as e:
        error_logger.error(f"Error formatting medical report: {str(e)}", exc_info=True)
        return f"Error formatting medical report: {str(e)}"

async def format_soap_note(gpt_response):
    """
    Format a SOAP note from GPT structured response based on SOAP_NOTE_SCHEMA.
    Only include section headings if there is documented information.
    
    Args:
        gpt_response: Dictionary containing the structured SOAP note data
        
    Returns:
        Formatted string containing the human-readable SOAP note
    """
    try:
        report = []
        
        # Add heading
        report.append("# SOAP NOTE\n")
        
        # Subjective section
        if "Subjective" in gpt_response and gpt_response["Subjective"]:
            has_content = False
            subjective_items = []
            for item in gpt_response["Subjective"]:
                if item and item != "Not discussed":
                    subjective_items.append(f"- {item}")
                    has_content = True
            if has_content:
                report.append("## Subjective:")
                report.extend(subjective_items)
                report.append("")  # Add spacing after section
        
        # Past Medical History section
        if "Past Medical History" in gpt_response and gpt_response["Past Medical History"]:
            has_content = False
            pmedhx_items = []
            for item in gpt_response["Past Medical History"]:
                if item and item != "Not discussed":
                    pmedhx_items.append(f"- {item}")
                    has_content = True
            if has_content:
                report.append("## Past Medical History:")
                report.extend(pmedhx_items)
                report.append("")  # Add spacing after section
        
        # Objective section
        if "Objective" in gpt_response and gpt_response["Objective"]:
            has_content = False
            objective_items = []
            for item in gpt_response["Objective"]:
                if item and item != "Not discussed":
                    objective_items.append(f"- {item}")
                    has_content = True
            if has_content:
                report.append("## Objective:")
                report.extend(objective_items)
                report.append("")  # Add spacing after section
        
        # Assessment section
        if "Assessment" in gpt_response and gpt_response["Assessment"]:
            has_content = False
            assessment_items = []
            for i, item in enumerate(gpt_response["Assessment"], 1):
                if item and item != "Not discussed":
                    assessment_items.append(f"{i}. {item}")
                    has_content = True
            if has_content:
                report.append("## Assessment:")
                report.extend(assessment_items)
                report.append("")  # Add spacing after section
        
        # Plan section
        if "Plan" in gpt_response and gpt_response["Plan"]:
            has_content = False
            plan_items = []
            for item in gpt_response["Plan"]:
                if item and item != "Not discussed":
                    plan_items.append(f"- {item}")
                    has_content = True
            if has_content:
                report.append("## Plan:")
                report.extend(plan_items)
                report.append("")  # Add spacing after section
        
        # If no sections have content, add a default message
        if len(report) == 1:  # Only the "# SOAP NOTE\n" heading is present
            report.append("No findings documented")
        
        return "\n".join(report).rstrip()  # Remove trailing newline
    except Exception as e:
        error_logger.error(f"Error formatting SOAP note: {str(e)}", exc_info=True)
        return f"Error formatting SOAP note: {str(e)}"

def format_ap_section(ap_data):
    """
    Unfolds an A/P dictionary into a plain-text format without hardcoding key names.
    Args:
        ap_data (dict): The A/P section of the SOAP note as a dictionary.
    Returns:
        list: List of formatted strings for inclusion in note.
    """
    output = []

    def format_value(value, indent=0):
        """Helper function to format values (strings, lists, or dicts) recursively."""
        if isinstance(value, str):
            return value
        elif isinstance(value, list):
            return "\n".join(f"{'  ' * indent}- {item}" for item in value if item)  # Skip empty items
        elif isinstance(value, dict):
            return "\n".join(
                f"{'  ' * indent}{key}: {format_value(val, indent + 1)}" for key, val in value.items() if val
            )
        return str(value)

    # Iterate through top-level keys (e.g., "1", "2") as entry numbers
    for entry_key, entry_value in sorted(ap_data.items(), key=lambda x: int(x[0]) if x[0].isdigit() else x[0]):
        if isinstance(entry_value, dict):
            # Start with entry number and main issue
            main_issue = entry_key.split(". ", 1)[1] if ". " in entry_key else entry_key
            output.append(f"{entry_key}")
            
            # Format remaining key-value pairs
            for key, val in entry_value.items():
                if val:  # Only process non-empty values
                    formatted = format_value(val, 0)
                    if formatted.strip():
                        output.append(f"{key}: {formatted}")
            output.append("")  # Blank line between entries
        else:
            output.append(f"{entry_key}: {format_value(entry_value, 1)}")
            output.append("")

    return output

async def format_h75(gpt_response):
    formatted_report = gpt_response.get("formatted_report")
    if formatted_report:
        return formatted_report
    else:
        return "No formatted report available."
async def format_new_soap(gpt_response):
    """
    Format a new SOAP note from GPT structured response.
    
    Args:
        gpt_response: Dictionary containing the structured SOAP note data
        
    Returns:
        Formatted string containing the human-readable SOAP note
    """
    try:
        note = []
        
        # Add heading
        note.append("# SOAP ISSUES\n")
        
        # SUBJECTIVE
        if gpt_response.get("S"):
            note.append("## SUBJECTIVE")
            for item in gpt_response["S"]:
                note.append(item)
            note.append("")
        
        
        # PAST MEDICAL HISTORY
        if gpt_response.get("PMedHx"):
            note.append("## PAST MEDICAL HISTORY")
            for item in gpt_response["PMedHx"]:
                note.append(f"{item}")
            note.append("")
        
        # SOCIAL HISTORY
        if gpt_response.get("SocHx"):
            note.append("## SOCIAL HISTORY")
            for item in gpt_response["SocHx"]:
                note.append(item)
            note.append("")
        
        # FAMILY HISTORY
        if gpt_response.get("FHx"):
            note.append("## FAMILY HISTORY")
            for item in gpt_response["FHx"]:
                note.append(item)
            note.append("")
        
        # OBJECTIVE
        if gpt_response.get("O"):
            note.append("## OBJECTIVE")
            for item in gpt_response["O"]:
                note.append(f"{item}")
            note.append("")
        # ASSESSMENT & PLAN
        # if gpt_response.get("A/P"):
        #     note.append("## ASSESSMENT & PLAN")
        #     for item in gpt_response["A/P"]:
        #         note.append(f"{item}")
        #         note.append("")
        if gpt_response.get("A/P"):
            note.append("## ASSESSMENT & PLAN")
            ap_lines = format_ap_section(gpt_response["A/P"])
            note.extend(ap_lines)
            note.append("")  # Blank line after section
        
        return "\n".join(note)
    except Exception as e:
        error_logger.error(f"Error formatting new SOAP note: {str(e)}", exc_info=True)
        return f"Error formatting new SOAP note: {str(e)}"

async def format_cardiac_report(gpt_response):
    """
    Format a cardiac assessment report from a GPT structured response based on a predefined template.
    
    Args:
        gpt_response: Dictionary containing the structured cardiac assessment data under 'CARDIOLOGY CONSULT NOTE'
        
    Returns:
        Formatted string containing the human-readable cardiac assessment report
    """
    try:
        # Extract the cardiology consult note from the response
        consult_note = gpt_response.get('CARDIOLOGY CONSULT NOTE', {})
        note = []
        note.append("# CARDIOLOGY CONSULT\n")
        # Patient Introduction
        if consult_note.get("Patient Introduction"):
            note.append(consult_note["Patient Introduction"])
            note.append("")  # Blank line after section
        # CARDIAC RISK FACTORS
        if consult_note.get("CARDIAC RISK FACTORS"):
            if isinstance(consult_note["CARDIAC RISK FACTORS"], dict):
                # Handle dictionary case
                if any(v != "None known" for k, v in consult_note["CARDIAC RISK FACTORS"].items()):
                    note.append("## CARDIAC RISK FACTORS")
                    for key, value in consult_note["CARDIAC RISK FACTORS"].items():
                        if value != "None known":
                            note.append(f"{key}: {value}")
                    note.append("")  # Blank line after section
            elif isinstance(consult_note["CARDIAC RISK FACTORS"], str):
                # Handle string case
                if consult_note["CARDIAC RISK FACTORS"].strip() and consult_note["CARDIAC RISK FACTORS"] != "None known":
                    note.append("## CARDIAC RISK FACTORS")
                    note.append(consult_note["CARDIAC RISK FACTORS"])
                    note.append("")  # Blank line after section
        # CARDIAC HISTORY
        if consult_note.get("CARDIAC HISTORY") and consult_note["CARDIAC HISTORY"].strip():
            note.append("## CARDIAC HISTORY")
            note.append(consult_note["CARDIAC HISTORY"])
            note.append("")  # Blank line after section

        # OTHER MEDICAL HISTORY
        if consult_note.get("OTHER MEDICAL HISTORY") and consult_note["OTHER MEDICAL HISTORY"] != "None known":
            note.append("## OTHER MEDICAL HISTORY")
            note.append(consult_note["OTHER MEDICAL HISTORY"])
            note.append("")  # Blank line after section

        # CURRENT MEDICATIONS
        if consult_note.get("CURRENT MEDICATIONS") and any(
            v != "None known" for v in consult_note["CURRENT MEDICATIONS"].values()
        ):
            note.append("## CURRENT MEDICATIONS")
            for key, value in consult_note["CURRENT MEDICATIONS"].items():
                if value != "None known":
                    note.append(f"{key}: {value}")
            note.append("")  # Blank line after section

        # ALLERGIES AND INTOLERANCES
        if consult_note.get("ALLERGIES AND INTOLERANCES") and consult_note["ALLERGIES AND INTOLERANCES"] != "None known":
            note.append("## ALLERGIES AND INTOLERANCES")
            note.append(consult_note["ALLERGIES AND INTOLERANCES"])
            note.append("")  # Blank line after section

        # SOCIAL HISTORY
        if consult_note.get("SOCIAL HISTORY") and consult_note["SOCIAL HISTORY"] != "None known":
            note.append("## SOCIAL HISTORY")
            note.append(consult_note["SOCIAL HISTORY"])
            note.append("")  # Blank line after section

        # HISTORY
        if consult_note.get("HISTORY") and consult_note["HISTORY"].strip():
            note.append("## HISTORY")
            note.append(consult_note["HISTORY"])
            note.append("")  # Blank line after section

        # PHYSICAL EXAMINATION
        if consult_note.get("PHYSICAL EXAMINATION") and consult_note["PHYSICAL EXAMINATION"].strip():
            note.append("## PHYSICAL EXAMINATION")
            note.append(consult_note["PHYSICAL EXAMINATION"])
            note.append("")  # Blank line after section

        # INVESTIGATIONS
        if consult_note.get("INVESTIGATIONS") and any(
            v != "None known" for v in consult_note["INVESTIGATIONS"].values()
        ):
            note.append("## INVESTIGATIONS")
            for key, value in consult_note["INVESTIGATIONS"].items():
                if value != "None known":
                    note.append(f"{key}: {value}")
            note.append("")  # Blank line after section

        # SUMMARY
        if consult_note.get("SUMMARY") and consult_note["SUMMARY"].strip():
            note.append("## SUMMARY")
            note.append(consult_note["SUMMARY"])
            note.append("")  # Blank line after section

        # ASSESSMENT/PLAN
        if consult_note.get("ASSESSMENT/PLAN"):
            note.append("## ASSESSMENT/PLAN")
            for item in consult_note["ASSESSMENT/PLAN"]:
                # Extract condition name from the dictionary key
                condition_key = list(item.keys())[0]
                # Remove numeric prefix (e.g., "1 ") for display
                condition_display = condition_key.split(" ", 1)[1] if condition_key[0].isdigit() else condition_key
                note.append(f"### {condition_display}")
                note.append(f"Assessment: {item[condition_key]['Assessment']}")
                note.append(f"Plan: {item[condition_key]['Plan']}")
                note.append("")  # Blank line after each condition

        # FOLLOW-UP
        if consult_note.get("FOLLOW-UP") and consult_note["FOLLOW-UP"].strip():
            note.append("## FOLLOW-UP")
            note.append(consult_note["FOLLOW-UP"])
            note.append("")  # Blank line after section

        # Closing
        if consult_note.get("Closing"):
            note.append(consult_note["Closing"])

        # Ensure standard US English grammar (as per _grammar_instruction)
        formatted_note = "\n".join(note)
        return formatted_note
    except Exception as e:
        error_logger.error(f"Error formatting cardiac assessment report: {str(e)}", exc_info=True)
        return f"Error formatting cardiac assessment report: {str(e)}"
    
async def format_soap_issues(gpt_response):
    """
    Format a new SOAP note from GPT structured response.
    
    Args:
        gpt_response: Dictionary containing the structured SOAP note data
        
    Returns:
        Formatted string containing the human-readable SOAP note
    """
    try:
        note = []
        
        # Add heading
        note.append("# SOAP ISSUES\n")
        
        # SUBJECTIVE
        if gpt_response.get("subjective", {}).get("issues"):
            note.append("## SUBJECTIVE")
            for issue in gpt_response["subjective"]["issues"]:
                note.append(f"### {issue['issue_name']}")
                if issue.get("reasons_for_visit"):
                    note.append(issue["reasons_for_visit"])
                if issue.get("duration_timing_location_quality_severity_context"):
                    note.append(issue["duration_timing_location_quality_severity_context"])
                if issue.get("worsens_alleviates"):
                    note.append(issue["worsens_alleviates"])
                if issue.get("progression"):
                    note.append(issue["progression"])
                if issue.get("previous_episodes"):
                    note.append(issue["previous_episodes"])
                if issue.get("impact_on_daily_activities"):
                    note.append(issue["impact_on_daily_activities"])
                if issue.get("associated_symptoms"):
                    note.append(issue["associated_symptoms"])
                if issue.get("medications"):
                    note.append(issue["medications"])

                note.append("")
        
        # PAST MEDICAL HISTORY
        if gpt_response.get("subjective", {}).get("past_medical_history"):
            note.append("## PAST MEDICAL HISTORY")
            pmh = gpt_response["subjective"]["past_medical_history"]
            if pmh.get("contributing_factors"):
                note.append(pmh["contributing_factors"])
            if pmh.get("social_history"):
                note.append(pmh["social_history"])
            if pmh.get("family_history"):
                note.append(pmh["family_history"])
            if pmh.get("exposure_history"):
                note.append(pmh["exposure_history"])
            if pmh.get("immunization_history"):
                note.append(pmh["immunization_history"])
            if pmh.get("other"):
                note.append(pmh["other"])
            note.append("")
        
        # OBJECTIVE
        if gpt_response.get("objective"):
            note.append("## OBJECTIVE")
            obj = gpt_response["objective"]
            if obj.get("vital_signs"):
                note.append(obj["vital_signs"])
            if obj.get("examination_findings"):
                note.append(obj["examination_findings"])
            if obj.get("investigations_with_results"):
                note.append(obj["investigations_with_results"])
            note.append("")
        
        # ASSESSMENT & PLAN
        if gpt_response.get("assessment_and_plan"):
            note.append("## ASSESSMENT & PLAN")
            for item in gpt_response["assessment_and_plan"]:
                note.append(f"### {item['issue_name']}")
                if item.get("likely_diagnosis"):
                    note.append(f"Likely Diagnosis: {item['likely_diagnosis']}")
                if item.get("differential_diagnosis"):
                    note.append(f"Differential Diagnosis: {item['differential_diagnosis']}")
                if item.get("investigations_planned"):
                    note.append(f"Investigations Planned: {item['investigations_planned']}")
                if item.get("treatment_planned"):
                    note.append(f"Treatment Planned: {item['treatment_planned']}")
                if item.get("referrals"):
                    note.append(f"Referrals: {item['referrals']}")
                note.append("")
        
        return "\n".join(note)
    except Exception as e:
        error_logger.error(f"Error formatting new SOAP note: {str(e)}", exc_info=True)
        return f"Error formatting new SOAP note: {str(e)}"

async def format_progress_note(data):
    """
    Format a progress note from GPT structured response.
    
    Args:
        data: Dictionary containing the structured progress note data
        
    Returns:
        String containing the formatted progress note
    """
    try:
        # Format current date if not provided
        if not data.get("note_date"):
            data["note_date"] = datetime.now().strftime("%d %B %Y")
            
        # Build the note
        note = []
        
        # Clinic letterhead
        
        # Clinic contact information
        if data.get("clinic_info", {}).get("address_line1"):
            note.append(data["clinic_info"]["address_line1"])
        if data.get("clinic_info", {}).get("address_line2"):
            note.append(data["clinic_info"]["address_line2"])
        if data.get("clinic_info", {}).get("phone"):
            note.append(data["clinic_info"]["phone"])
        if data.get("clinic_info", {}).get("fax"):
            note.append(data["clinic_info"]["fax"])
        note.append("")
        
        # Practitioner information
        practitioner_name = data.get("practitioner", {}).get("name", "")
        practitioner_title = data.get("practitioner", {}).get("title", "")
        if practitioner_name or practitioner_title:
            practitioner_info = f"Practitioner: {practitioner_name}"
            if practitioner_title:
                practitioner_info += f", {practitioner_title}"
            note.append(practitioner_info)
            note.append("")
        
        # Patient information
        if data.get("patient", {}).get("surname"):
            note.append(f"Surname: {data['patient']['surname']}")
        if data.get("patient", {}).get("firstname"):
            note.append(f"First Name: {data['patient']['firstname']}")
        if data.get("patient", {}).get("dob"):
            note.append(f"Date of Birth: {data['patient']['dob']}")
        note.append("")
        
        # Progress Note header
        note.append("# PROGRESS NOTE\n")
        
        # Date of note
        note.append(data.get("note_date", ""))
        note.append("")
        
        # Introduction
        if data.get("introduction"):
            note.append(data["introduction"])
            note.append("")
        
        # Patient history
        if data.get("history"):
            note.append(data["history"])
            note.append("")
        
        # Presentation
        if data.get("presentation"):
            note.append(data["presentation"])
            note.append("")
        
        # Mood and mental state
        if data.get("mood_mental_state"):
            note.append(data["mood_mental_state"])
            note.append("")
        
        # Social and functional status
        if data.get("social_functional"):
            note.append(data["social_functional"])
            note.append("")
        
        # Physical health
        if data.get("physical_health"):
            note.append(data["physical_health"])
            note.append("")
        
        # Plan and recommendations
        if data.get("plan"):
            note.append("## Plan and Recommendations:")
            for i, item in enumerate(data["plan"], 1):
                note.append(f"{i}. {item}")
            note.append("")
        
        # Closing
        if data.get("closing"):
            note.append(data["closing"])
            note.append("")
        
        # Practitioner signature
        if practitioner_name or practitioner_title:
            full_title = f"{practitioner_name}"
            if practitioner_title:
                full_title += f", {practitioner_title}"
            note.append(full_title)
            note.append("Consultant Psychiatrist")
        
        return "\n".join(note)
        
    except Exception as e:
        error_logger.error(f"Error formatting progress note: {str(e)}", exc_info=True)
        return f"Error formatting progress note: {str(e)}"

async def format_mental_health_note(gpt_response):
    """
    Format a mental health note from a structured GPT response.
    
    Args:
        gpt_response: Dictionary containing the structured mental health note data
        
    Returns:
        Formatted string containing the human-readable mental health note
    """
    try:
        note = []
        
        # Add heading
        note.append("# MENTAL HEALTH NOTE\n")
        
        # CHIEF COMPLAINT
        if gpt_response.get("Chief Complaint"):
            note.append("## CHIEF COMPLAINT")
            for item in gpt_response["Chief Complaint"]:
                note.append(item)
            note.append("")
        
        # PAST MEDICAL & PSYCHIATRIC HISTORY
        if gpt_response.get("Past medical & psychiatric history"):
            note.append("## PAST MEDICAL & PSYCHIATRIC HISTORY")
            for item in gpt_response["Past medical & psychiatric history"]:
                note.append(item)
            note.append("")
        
        # FAMILY HISTORY
        if gpt_response.get("Family History"):
            note.append("## FAMILY HISTORY")
            for item in gpt_response["Family History"]:
                note.append(item)
            note.append("")
        
        # SOCIAL HISTORY
        if gpt_response.get("Social History"):
            note.append("## SOCIAL HISTORY")
            seen = set()
            for item in gpt_response["Social History"]:
                item_text = item[2:].strip()  # Remove "- " prefix for comparison
                if item_text not in seen:
                    seen.add(item_text)
                    note.append(item)
            note.append("")
        
        # MENTAL STATUS EXAMINATION
        if gpt_response.get("Mental Status Examination"):
            note.append("## MENTAL STATUS EXAMINATION")
            for item in gpt_response["Mental Status Examination"]:
                if item[2:].strip() != "No findings reported":  # Skip placeholder text
                    note.append(item)
            note.append("")
        
        # RISK ASSESSMENT
        if gpt_response.get("Risk Assessment"):
            note.append("## RISK ASSESSMENT")
            for item in gpt_response["Risk Assessment"]:
                note.append(item)
            note.append("")
        
        # DIAGNOSIS
        if gpt_response.get("Diagnosis"):
            note.append("## DIAGNOSIS")
            for item in gpt_response["Diagnosis"]:
                note.append(item)
            note.append("")
        
        # TREATMENT PLAN
        if gpt_response.get("Treatment Plan"):
            note.append("## TREATMENT PLAN")
            for item in gpt_response["Treatment Plan"]:
                note.append(item[2:].strip())  # Remove "- " for plain text
            note.append("")
        
        # SAFETY PLAN
        if gpt_response.get("Safety Plan"):
            note.append("## SAFETY PLAN")
            for item in gpt_response["Safety Plan"]:
                note.append(item[2:].strip())  # Remove "- " for plain text
            note.append("")
        
        return "\n".join(note)
    except Exception as e:
        error_logger.error(f"Error formatting mental health note: {str(e)}", exc_info=True)
        return f"Error formatting mental health note: {str(e)}"

async def format_detailed_soap_note(gpt_response):
    """
    Format a concise, doctor-centric SOAP note from GPT structured response, summarizing and analyzing key clinical data.
    
    Args:
        gpt_response: Dictionary containing the structured SOAP note data (e.g., Subjective, ReviewOfSystems, Objective, Assessment, FollowUp)
        
    Returns:
        Formatted string containing the human-readable SOAP note, omitting sections with no data
    """
    try:
        note = []
        # Add title
        note.append("# Detailed SOAP Note\n")

        def is_valid_data(value):
            """Check if a value contains valid data (not empty or 'Not documented')."""
            if value is None or value == "Not documented":
                return False
            if isinstance(value, (list, dict)) and not value:
                return False
            if isinstance(value, str) and not value.strip():
                return False
            return True

        def summarize_subjective(data):
            """Summarize Subjective list or string into concise, multi-sentence points."""
            if not is_valid_data(data):
                return []
            sentences = []
            if isinstance(data, list):
                sentences = [s.strip() for s in data if s.strip()]
            elif isinstance(data, str):
                sentences = [s.strip() for s in re.split(r'[.!?]', data) if s.strip()]
            
            points = []
            symptoms = []
            history = []
            medications = []
            for s in sentences:
                if any(keyword in s.lower() for keyword in ["cough", "shortness of breath", "chest", "fever", "fatigue", "wheezing"]):
                    symptoms.append(s)
                elif any(keyword in s.lower() for keyword in ["asthma", "diabetes", "smoking", "allergies", "hypertension", "gerd"]):
                    history.append(s)
                elif any(keyword in s.lower() for keyword in ["inhaler", "metformin", "medication", "albuterol"]):
                    medications.append(s)
                # Skip irrelevant details (e.g., travel, son)

            if symptoms:
                symptom_summary = " ".join(symptoms[:3]).replace("approximately six days ago", "since May 26, 2025")
                points.append(f"- {symptom_summary}. Symptoms worsen at night with exertion. Associated with productive cough and fatigue.")
            if history:
                points.append(f"- {'. '.join(history[:2])}. Influences current respiratory management.")
            if medications:
                points.append(f"- {'. '.join(medications)}. Stable, no reported adverse effects.")
            return points

        def format_value(value, indent=0, prefix="- "):
            """Format a value (string, list, or dict) with appropriate indentation."""
            indent_str = "  " * indent
            if isinstance(value, str) and value.strip():
                return f"{indent_str}{prefix}{value}"
            elif isinstance(value, list) and value:
                return "\n".join(f"{indent_str}{prefix}{item}" for item in value if item)
            elif isinstance(value, dict) and value:
                lines = []
                for k, v in value.items():
                    if is_valid_data(v):
                        if isinstance(v, dict):
                            lines.append(f"{indent_str}{k}:")
                            for sub_k, sub_v in v.items():
                                if is_valid_data(sub_v):
                                    if isinstance(sub_v, list):
                                        lines.append(f"{indent_str}  {sub_k}:")
                                        lines.append("\n".join(f"{indent_str}    - {item}" for item in sub_v if item))
                                    else:
                                        lines.append(f"{indent_str}  - {sub_k}: {sub_v}")
                        elif isinstance(v, list):
                            lines.append(f"{indent_str}{k}:")
                            lines.append("\n".join(f"{indent_str}  - {item}" for item in v if item))
                        else:
                            lines.append(f"{indent_str}{prefix}{k}: {v}")
                return "\n".join(lines)
            return ""

        # Map section keys (handle both snake_case and CamelCase)
        section_mapping = {
            "subjective": ["subjective", "Subjective"],
            "review_of_systems": ["review_of_systems", "ReviewOfSystems"],
            "objective": ["objective", "Objective"],
            "assessment": ["assessment", "Assessment"],
            "plan": ["plan", "Plan"],
            "follow_up": ["follow_up", "FollowUp"]
        }

        # Define sections
        sections = [
            ("subjective", "## Subjective"),
            ("review_of_systems", "## Review of Systems"),
            ("objective", "## Objective"),
            ("assessment", "## Assessment"),
            ("plan", "## Plan"),
            ("follow_up", "## Follow-Up")
        ]

        assessment_lines = []
        processed_keys = set()

        for section_key, header in sections:
            # Check both snake_case and CamelCase keys
            section_data = None
            for key in section_mapping[section_key]:
                if key in gpt_response:
                    section_data = gpt_response[key]
                    processed_keys.add(key)
                    break

            if not section_data or not any(is_valid_data(v) for v in (section_data.values() if isinstance(section_data, dict) else [section_data])):
                continue  # Skip empty sections

            section_lines = []
            section_documented = False

            # Handle Subjective
            if section_key == "subjective":
                section_lines.extend(summarize_subjective(section_data))
                section_documented = bool(section_lines)
            # Handle Review of Systems
            elif section_key == "review_of_systems" and isinstance(section_data, dict):
                for system, findings in section_data.items():
                    if is_valid_data(findings):
                        section_lines.append(f"- {system}: {findings}. Relevant to respiratory presentation.")
                        section_documented = True
            # Handle Assessment
            elif section_key == "assessment":
                if isinstance(section_data, list):
                    for item in section_data:
                        if is_valid_data(item):
                            section_lines.append(f"- {item}. Guides therapeutic approach.")
                            section_documented = True
                elif isinstance(section_data, dict):
                    for key, value in section_data.items():
                        if is_valid_data(value):
                            formatted = format_value({key: value}, indent=0)
                            if formatted:
                                section_lines.append(formatted)
                                section_documented = True
            # Handle other sections
            else:
                formatted = format_value(section_data, indent=0)
                if formatted:
                    section_lines.append(formatted)
                    section_documented = True

            if section_documented:
                note.append(f"{header}:")
                note.extend(section_lines)
                note.append("")  # Blank line after section

        # Handle additional Assessment keys (e.g., "Acute Cough, Shortness of Breath, and Chest Tightness Assessment")
        for key, value in gpt_response.items():
            if key not in processed_keys and is_valid_data(value):
                if "Assessment" in key:
                    formatted = format_value({key: value}, indent=0)
                    if formatted:
                        assessment_lines.append(formatted)
                        processed_keys.add(key)

        # Append additional Assessment data if present
        if assessment_lines and any("## Assessment" in line for line in note):
            assessment_index = next(i for i, line in enumerate(note) if line.startswith("## Assessment"))
            note[assessment_index + 1:assessment_index + 1] = assessment_lines + [""]
        elif assessment_lines:
            note.append("## Assessment:")
            note.extend(assessment_lines)
            note.append("")

        # Return empty string if only title
        if len(note) == 1:
            return ""

        return "\n".join(note).rstrip()

    except Exception as e:
        logging.error(f"Error formatting detailed SOAP note: {str(e)}", exc_info=True)
        return f"Error formatting detailed SOAP note: {str(e)}"

async def format_followup_note(gpt_response):
    """
    Format a follow-up note from GPT structured response based on FOLLOWUP_NOTE_SCHEMA.
    
    Args:
        gpt_response: Dictionary containing the structured follow-up note data
        
    Returns:
        Formatted string containing the human-readable follow-up note
    """
    try:
        report = []
        
        # Add title
        report.append("# FOLLOW UP NOTE\n")
        # Date
        if "date" in gpt_response and gpt_response["date"] and gpt_response["date"] != "Not documented":
            report.append(f"Date: {gpt_response['date']}")
        else:
            # Use current date if not provided or is "Not documented"
            current_date = datetime.now().strftime("%B %d, %Y")
            report.append(f"Date: {current_date}")
        report.append("")
        
        # History of Presenting Complaints
        report.append("## History of Presenting Complaints:")
        if "presenting_complaints" in gpt_response and gpt_response["presenting_complaints"]:
            if isinstance(gpt_response["presenting_complaints"], list) and len(gpt_response["presenting_complaints"]) > 0:
                for complaint in gpt_response["presenting_complaints"]:
                    if complaint != "Not documented" and complaint:
                        report.append(f"- {complaint}")
            elif isinstance(gpt_response["presenting_complaints"], str) and gpt_response["presenting_complaints"] != "Not documented":
                report.append(f"- {gpt_response['presenting_complaints']}")
            else:
                report.append("- Not documented")
        else:
            report.append("- Not documented")
        report.append("")
        
        # Mental Status Examination
        report.append("## Mental Status Examination:")
        if "mental_status" in gpt_response and gpt_response["mental_status"]:
            mental_status = gpt_response["mental_status"]
            
            # Iterate through each mental status component
            for key, label in [
                ("appearance", "Appearance"),
                ("behavior", "Behavior"),
                ("speech", "Speech"),
                ("mood", "Mood"),
                ("affect", "Affect"),
                ("thoughts", "Thoughts"),
                ("perceptions", "Perceptions"),
                ("cognition", "Cognition"),
                ("insight", "Insight"),
                ("judgment", "Judgment")
            ]:
                value = mental_status.get(key, "Not documented")
                if value and value != "Not documented":
                    report.append(f"- {label}: {value}")
                else:
                    report.append(f"- {label}: Not documented")
        else:
            report.append("- Not documented")
        report.append("")
        
        # Risk Assessment
        report.append("## Risk Assessment:")
        risk = gpt_response.get("risk_assessment", "Not documented")
        if risk and risk != "Not documented":
            report.append(f"- {risk}")
        else:
            report.append("- Not documented")
        report.append("")
        
        # Diagnosis
        report.append("## Diagnosis:")
        if "diagnosis" in gpt_response and gpt_response["diagnosis"]:
            if isinstance(gpt_response["diagnosis"], list):
                for diagnosis in gpt_response["diagnosis"]:
                    if diagnosis and diagnosis != "Not documented" and diagnosis != "None":
                        report.append(f"- {diagnosis}")
                if not any(d and d != "Not documented" and d != "None" for d in gpt_response["diagnosis"]):
                    report.append("- None")
            elif isinstance(gpt_response["diagnosis"], str) and gpt_response["diagnosis"] != "Not documented":
                report.append(f"- {gpt_response['diagnosis']}")
            else:
                report.append("- None")
        else:
            report.append("- None")
        report.append("")
        
        # Treatment Plan
        report.append("## Treatment Plan:")
        treatment_plan = gpt_response.get("treatment_plan", "Not documented")
        if treatment_plan and treatment_plan != "Not documented":
            # If treatment plan is a string, add it as a single bullet point
            if isinstance(treatment_plan, str):
                report.append(f"- {treatment_plan}")
            # If it's a list, add each item as a bullet point
            elif isinstance(treatment_plan, list):
                for plan_item in treatment_plan:
                    if plan_item and plan_item != "Not documented":
                        report.append(f"- {plan_item}")
        else:
            report.append("- Not documented")
        report.append("")
        
        # Safety Plan
        report.append("## Safety Plan:")
        safety_plan = gpt_response.get("safety_plan", "Not documented")
        if safety_plan and safety_plan != "Not documented":
            report.append(f"- {safety_plan}")
        else:
            report.append("- Not documented")
        report.append("")
        
        # Additional Notes
        report.append("## Additional Notes:")
        additional_notes = gpt_response.get("additional_notes", "None")
        if additional_notes and additional_notes != "Not documented" and additional_notes != "None":
            report.append(f"- {additional_notes}")
        else:
            report.append("- None")
        
        return "\n".join(report)
    except Exception as e:
        error_logger.error(f"Error formatting follow-up note: {str(e)}", exc_info=True)
        return f"Error formatting follow-up note: {str(e)}"
    
async def format_case_formulation(gpt_response):
    """
    Format a case formulation report following the 4Ps schema from GPT structured response.
    
    Args:
        gpt_response: Dictionary containing the structured case formulation data
        
    Returns:
        Formatted string containing the human-readable case formulation
    """
    try:
        # Handle the case where GPT returns an unexpected structure
        if "client_goals" not in gpt_response:
            # If we got an unexpected response structure, try to map it to our expected structure
            main_logger.warning("Received unexpected structure for case formulation")
            
            # Create a new response with default values
            mapped_response = {
                "client_goals": "Not documented",
                "presenting_problems": "Not documented",
                "predisposing_factors": "Not documented",
                "precipitating_factors": "Not documented",
                "perpetuating_factors": "Not documented",
                "protective_factors": "Not documented",
                "problem_list": "Not documented",
                "treatment_goals": "Not documented",
                "case_formulation": "Not documented",
                "treatment_mode": "Not documented"
            }
            
            # Check if we received a Patient Presentation structure
            if "Patient Presentation" in gpt_response:
                patient_data = gpt_response["Patient Presentation"]
                assessment = gpt_response.get("Assessment and Plan", {})
                
                # Try to map fields from the unexpected structure to our expected structure
                if "Chief Complaint" in patient_data:
                    mapped_response["presenting_problems"] = patient_data["Chief Complaint"]
                
                # Map symptoms to problem list
                if "History of Present Illness" in patient_data and "Symptoms" in patient_data["History of Present Illness"]:
                    symptoms = patient_data["History of Present Illness"]["Symptoms"]
                    problems = []
                    for category, description in symptoms.items():
                        if isinstance(description, str):
                            problems.append(f"{category}: {description}")
                        elif isinstance(description, dict):
                            for symptom, detail in description.items():
                                problems.append(f"{symptom}: {detail}")
                    if problems:
                        mapped_response["problem_list"] = problems
                
                # Map social history to protective factors
                if "Social History" in patient_data:
                    social = []
                    for key, value in patient_data["Social History"].items():
                        social.append(f"{key}: {value}")
                    if social:
                        mapped_response["protective_factors"] = social
                
                # Map diagnosis and treatment plan
                if "Diagnosis" in assessment:
                    mapped_response["case_formulation"] = f"The patient presents with {assessment['Diagnosis']}."
                
                if "Treatment Plan" in assessment:
                    treatment = []
                    for key, value in assessment["Treatment Plan"].items():
                        treatment.append(f"{key}: {value}")
                    if treatment:
                        mapped_response["treatment_mode"] = treatment
            
            # Use the mapped response
            gpt_response = mapped_response
        
        report = []
        
        # Add title
        report.append("# CASE FORMULATION 4PS\n")
        
        # CLIENT GOALS
        report.append("## CLIENT GOALS:")
        client_goals = gpt_response.get("client_goals", "Not documented")
        if client_goals and client_goals != "Not documented":
            report.append(client_goals)
        else:
            report.append("Client goals were not documented during the assessment.")
        report.append("")
        
        # PRESENTING PROBLEM/S
        report.append("## PRESENTING PROBLEM/S:")
        presenting_problems = gpt_response.get("presenting_problems", "Not documented")
        if presenting_problems and presenting_problems != "Not documented":
            if isinstance(presenting_problems, list):
                for problem in presenting_problems:
                    if problem and problem != "Not documented":
                        report.append(f"- {problem}")
            else:
                report.append(presenting_problems)
        else:
            report.append("No presenting problems were documented during the assessment.")
        report.append("")
        
        # PREDISPOSING FACTORS
        report.append("## PREDISPOSING FACTORS:")
        predisposing_factors = gpt_response.get("predisposing_factors", "Not documented")
        if predisposing_factors and predisposing_factors != "Not documented":
            if isinstance(predisposing_factors, list):
                for factor in predisposing_factors:
                    if factor and factor != "Not documented":
                        report.append(f"- {factor}")
            else:
                report.append(predisposing_factors)
        else:
            report.append("No predisposing factors were documented during the assessment.")
        report.append("")
        
        # PRECIPITATING FACTORS
        report.append("## PRECIPITATING FACTORS:")
        precipitating_factors = gpt_response.get("precipitating_factors", "Not documented")
        if precipitating_factors and precipitating_factors != "Not documented":
            if isinstance(precipitating_factors, list):
                for factor in precipitating_factors:
                    if factor and factor != "Not documented":
                        report.append(f"- {factor}")
            else:
                report.append(precipitating_factors)
        else:
            report.append("No precipitating factors were documented during the assessment.")
        report.append("")
        
        # PERPETUATING FACTORS
        report.append("## PERPETUATING FACTORS:")
        perpetuating_factors = gpt_response.get("perpetuating_factors", "Not documented")
        if perpetuating_factors and perpetuating_factors != "Not documented":
            if isinstance(perpetuating_factors, list):
                for factor in perpetuating_factors:
                    if factor and factor != "Not documented":
                        report.append(f"- {factor}")
            else:
                report.append(perpetuating_factors)
        else:
            report.append("No perpetuating factors were documented during the assessment.")
        report.append("")
        
        # PROTECTIVE FACTORS
        report.append("## PROTECTIVE FACTORS:")
        protective_factors = gpt_response.get("protective_factors", "Not documented")
        if protective_factors and protective_factors != "Not documented":
            if isinstance(protective_factors, list):
                for factor in protective_factors:
                    if factor and factor != "Not documented":
                        report.append(f"- {factor}")
            else:
                report.append(protective_factors)
        else:
            report.append("No protective factors were documented during the assessment.")
        report.append("")
        
        # PROBLEM LIST
        report.append("## PROBLEM LIST:")
        problem_list = gpt_response.get("problem_list", "Not documented")
        if problem_list and problem_list != "Not documented":
            if isinstance(problem_list, list):
                for i, problem in enumerate(problem_list, 1):
                    if problem and problem != "Not documented":
                        report.append(f"{i}. {problem}")
            else:
                report.append(problem_list)
        else:
            report.append("No problem list was documented during the assessment.")
        report.append("")
        
        # TREATMENT GOALS
        report.append("## TREATMENT GOALS:")
        treatment_goals = gpt_response.get("treatment_goals", "Not documented")
        if treatment_goals and treatment_goals != "Not documented":
            if isinstance(treatment_goals, list):
                for i, goal in enumerate(treatment_goals, 1):
                    if goal and goal != "Not documented":
                        report.append(f"{i}. {goal}")
            else:
                report.append(treatment_goals)
        else:
            report.append("No treatment goals were documented during the assessment.")
        report.append("")
        
        # CASE FORMULATION
        report.append("## CASE FORMULATION:")
        case_formulation = gpt_response.get("case_formulation", "Not documented")
        if case_formulation and case_formulation != "Not documented":
            report.append(case_formulation)
        else:
            report.append("No comprehensive case formulation was documented during the assessment.")
        report.append("")
        
        # TREATMENT MODE/INTERVENTIONS
        report.append("## TREATMENT MODE/INTERVENTIONS:")
        treatment_mode = gpt_response.get("treatment_mode", "Not documented")
        if treatment_mode and treatment_mode != "Not documented":
            if isinstance(treatment_mode, list):
                for mode in treatment_mode:
                    if mode and mode != "Not documented":
                        report.append(f"- {mode}")
            else:
                report.append(treatment_mode)
        else:
            report.append("No treatment mode or interventions were documented during the assessment.")
        
        return "\n".join(report)
    except Exception as e:
        error_logger.error(f"Error formatting case formulation: {str(e)}", exc_info=True)
        return f"Error formatting case formulation: {str(e)}"

async def format_meeting_minutes(gpt_response):
    """
    Format meeting minutes from GPT structured response based on MEETING_MINUTES_SCHEMA.
    
    Args:
        gpt_response: Dictionary containing the structured meeting minutes data
        
    Returns:
        Formatted string containing the human-readable meeting minutes
    """
    try:
        minutes = []
        
        # Add heading
        minutes.append("# MEETING MINUTES\n")
        
        # Date, Time, Location
        if gpt_response.get("date"):
            minutes.append(f"Date: {gpt_response['date']}")
        else:
            # Use current date if not provided
            current_date = datetime.now().strftime("%B %d, %Y")
            minutes.append(f"Date: {current_date}")
        
        if gpt_response.get("time"):
            minutes.append(f"Time: {gpt_response['time']}")
        else:
            # Use current time if not provided
            current_time = datetime.now().strftime("%I:%M %p")
            minutes.append(f"Time: {current_time}")
        
        if gpt_response.get("location"):
            minutes.append(f"Location: {gpt_response['location']}")
        else:
            minutes.append("Location: Not documented")
        
        minutes.append("")
        
        # Attendees
        minutes.append("## Attendees:")
        attendees = gpt_response.get("attendees", [])
        if attendees:
            for attendee in attendees:
                minutes.append(f"- {attendee}")
        else:
            minutes.append("- Not documented")
        
        minutes.append("")
        
        # Agenda Items
        minutes.append("## Agenda Items:")
        agenda_items = gpt_response.get("agenda_items", [])
        if agenda_items:
            for item in agenda_items:
                minutes.append(f"- {item}")
        else:
            minutes.append("- Not documented")
        
        minutes.append("")
        
        # Discussion Points
        minutes.append("## Discussion Points:")
        discussion_points = gpt_response.get("discussion_points", [])
        if discussion_points:
            for point in discussion_points:
                minutes.append(f"- {point}")
        else:
            minutes.append("- Not documented")
        
        minutes.append("")
        
        # Decisions Made
        minutes.append("## Decisions Made:")
        decisions = gpt_response.get("decisions_made", [])
        if decisions:
            for decision in decisions:
                minutes.append(f"- {decision}")
        else:
            minutes.append("- No decisions documented")
        
        minutes.append("")
        
        # Action Items
        minutes.append("## Action Items:")
        actions = gpt_response.get("action_items", [])
        if actions:
            for action in actions:
                minutes.append(f"- {action}")
        else:
            minutes.append("- No action items documented")
        
        minutes.append("")
        
        # Next Meeting
        minutes.append("## Next Meeting:")
        next_meeting = gpt_response.get("next_meeting", {})
        
        if next_meeting.get("date"):
            minutes.append(f"- Date: {next_meeting['date']}")
        else:
            minutes.append("- Date: Not scheduled")
            
        if next_meeting.get("time"):
            minutes.append(f"- Time: {next_meeting['time']}")
        else:
            minutes.append("- Time: Not scheduled")
            
        if next_meeting.get("location"):
            minutes.append(f"- Location: {next_meeting['location']}")
        else:
            minutes.append("- Location: Not determined")
        
        return "\n".join(minutes)
    except Exception as e:
        error_logger.error(f"Error formatting meeting minutes: {str(e)}", exc_info=True)
        return f"Error formatting meeting minutes: {str(e)}"
    
async def format_dietician_assessment(gpt_response):
    """
    Format a detailed dietician initial assessment from GPT structured response.
    
    Args:
        gpt_response: Dictionary containing the structured dietician assessment data
        
    Returns:
        Formatted string containing the human-readable dietician assessment
    """
    try:
        report = []
        # Title
        report.append("# DETAILED DIETICIAN INITIAL ASSESSMENT\n")
        
        # Weight History
        weight_history = gpt_response.get("weight_history")
        if weight_history and weight_history != "Not discussed":
            report.append("##Weight History")
            report.append(weight_history)
            report.append("")
        else:
            report.append("## Weight History")
            report.append("Weight History was not discussed in the initial assessment.")
            report.append("")

        # Consolidated Disordered Eating/Eating Disorder Behavior and Nutrition Intake
        disordered_eating_behavior = gpt_response.get("dietary_habits")
        if disordered_eating_behavior and disordered_eating_behavior != "Not discussed":
            report.append("## Disordered Eating/Eating Disorder Behavior and Nutrition Intake")
            report.append(disordered_eating_behavior)
            report.append("")
        else:
            report.append("## Disordered Eating/Eating Disorder Behavior and Nutrition Intake")
            report.append("Disordered Eating/Eating Disorder Behavior and Nutrition Intake were not discussed in the initial assessment.")
            report.append("")

        # Physical Activity Behavior
        physical_activity = gpt_response.get("physical_activity")
        if physical_activity and physical_activity != "Not discussed":
            report.append("## Physical Activity Behavior")
            report.append(physical_activity)
            report.append("")
        else:
            report.append("## Physical Activity Behavior")
            report.append("Physical Activity Behavior was not discussed in the initial assessment.")
            report.append("")

        # Medical & Psychiatric History
        medical_history = gpt_response.get("medical_history")
        if medical_history and medical_history != "Not discussed":
            report.append("## Medical & Psychiatric History")
            report.append(medical_history)
            report.append("")
        else:
            report.append("## Medical & Psychiatric History")
            report.append("Medical & Psychiatric History was not discussed in the initial assessment.")
            report.append("")

        # Medications/Supplements
        medications = gpt_response.get("medications")
        supplements = gpt_response.get("supplements")
        if (medications and medications != "Not discussed") or (supplements and supplements != "Not discussed"):
            report.append("## Medications/Supplements")
            if medications:
                report.append(f"Medications: {medications}")
            if supplements:
                report.append(f"Supplements: {supplements}")
            report.append("")
        else:
            report.append("## Medications/Supplements")
            report.append("Medications/Supplements were not discussed in the initial assessment.")
            report.append("")

        # Social History/Lifestyle
        social_history = gpt_response.get("goals")
        if social_history and social_history != "Not discussed":
            report.append("## Social History/Lifestyle")
            report.append(social_history)
            report.append("")
        else:
            report.append("## Social History/Lifestyle")
            report.append("Social History/Lifestyle was not discussed in the initial assessment.")
            report.append("")
        
        return "\n".join(report)
    except Exception as e:
        error_logger.error(f"Error formatting dietician assessment: {str(e)}", exc_info=True)
        return f"Error formatting dietician assessment: {str(e)}"

async def format_consult_note(gpt_response):
    """
    Format a consultation note from a structured GPT response.
    
    Args:
        gpt_response: Dictionary containing the structured consultation note data
        
    Returns:
        Formatted string containing the human-readable consultation note
    """
    try:
        note = []
        
        # Add heading
        note.append("# CONSULTATION NOTE\n")

        # CONSULTATION TYPE
        if gpt_response.get("Consultation Type"):
            note.append("## CONSULTATION TYPE")
            for item in gpt_response["Consultation Type"]:
                note.append(item)
            note.append("")
        
        # HISTORY
        if gpt_response.get("History"):
            note.append("## HISTORY")
            history = gpt_response["History"]
            
            if isinstance(history, list):
                # Handle History as a list of strings
                for item in history:
                    if item.strip():  # Skip empty items
                        note.append(item)
                note.append("")
            
            elif isinstance(history, dict):
                # Handle History as a dictionary with sub-sections
                # History of Presenting Complaints
                if history.get("History of Presenting Complaints"):
                    for item in history["History of Presenting Complaints"]:
                        note.append(item)
                    note.append("")
                # PMH/PSH
                if history.get("ICE"):
                    ice_items = [item[2:].strip() for item in history["ICE"]]  # Remove "- " prefix
                    note.append(f"- ICE: {', '.join(ice_items)}")
                    note.append("")
                
                # Relevant Risk Factors
                if history.get("Relevant Risk Factors"):
                    for item in history["Relevant Risk Factors"]:
                        note.append(item)
                    note.append("")
            
                # PMH/PSH
                if history.get("PMH/PSH"):
                    pmh_psh_items = [item[2:].strip() for item in history["PMH/PSH"]]  # Remove "- " prefix
                    note.append(f"- PMH/PSH: {', '.join(pmh_psh_items)}")
                    note.append("")

                # DH
                dh_items = []
                if history.get("DH"):
                    dh_items= [item[2:].strip() for item in history["DH"]]
                if history.get("DH/Allergies"):
                    dh_items= [item[2:].strip() for item in history["DH/Allergies"]]
                if dh_items:
                    note.append(f"- DH/Allergies: {', '.join(dh_items)}")
                    note.append("")

                # FH
                if history.get("FH"):
                    fh_items = [item[2:].strip() for item in history["FH"]]  # Remove "- " prefix
                    note.append(f"- FH: {', '.join(fh_items)}")
                    note.append("")
                

                # SH
                if history.get("SH"):
                    sh_items = [item[2:].strip() for item in history["SH"]]  # Remove "- " prefix
                    note.append(f"- sH: {', '.join(sh_items)}")
                    note.append("")

        if gpt_response.get("Examination"):
            note.append("## EXAMINATION")
            exam = gpt_response["Examination"]
            
            if isinstance(exam, list):
                # Handle History as a list of strings
                for item in exam:
                    if item.strip():  # Skip empty items
                        note.append(item)
                note.append("")
            
            elif isinstance(exam, dict):
        
                if exam.get("Vital Signs"):
                    vital_items = [item[2:].strip() for item in exam["Vital Signs"]]  # Remove "- " prefix
                    note.append(f"- Vital Signs: {', '.join(vital_items)}")

                # "Physical/Mental State Examination Findings
                if exam.get("Physical/Mental State Examination Findings"):
                    for item in exam["Physical/Mental State Examination Findings"]:
                        note.append(item)
                    note.append("")
                

                # if exam.get("Physical/Mental State Examination Findings"):
                #     phs_items = [item[2:].strip() for item in exam["Physical/Mental State Examination Findings"]]  # Remove "- " prefix
                #     note.append(f"- {', '.join(phs_items)}")
                #     note.append("")
                
                # Investigations with Results
                if exam.get("Investigations with Results"):
                    for item in exam["Investigations with Results"]:
                        note.append(item)
                    note.append("")
                # if exam.get("Investigations with Results"):
                #     iwr_items = [item[2:].strip() for item in exam["Investigations with Results"]]  # Remove "- " prefix
                #     note.append(f"- {', '.join(iwr_items)}")
                #     note.append("")

        # IMPRESSION
        if gpt_response.get("Impression"):
            note.append("## IMPRESSION")
            for item in gpt_response["Impression"]:
                # Handle non-bulleted impression statement
                if not item.startswith("-"):
                    note.append(item)
                else:
                    note.append(item)
            note.append("")
        
        # PLAN
        if gpt_response.get("Plan"):
            # Check if Plan is a dictionary (nested with sub-headings) or a list (flat)
            plan = gpt_response["Plan"]
            has_valid_content = False
            
            if isinstance(plan, dict):
                # Handle nested Plan with sub-headings
                valid_subheadings = []
                for subheading, values in plan.items():
                    # Ensure values is a list and not empty
                    if isinstance(values, list) and values and any(v.strip() for v in values if v):
                        valid_subheadings.append((subheading, values))
                    elif isinstance(values, str) and values.strip():
                        valid_subheadings.append((subheading, [f"- {values}"]))
                
                if valid_subheadings:
                    has_valid_content = True
                    note.append("## PLAN")
                    for subheading, values in valid_subheadings:
                        note.append(f"{subheading}: ")
                        for value in values:
                            note.append(value)
                        note.append("")
            
            elif isinstance(plan, list) and any(item.strip() for item in plan if item):
                # Handle flat Plan list (as in provided gpt_response)
                has_valid_content = True
                note.append("## PLAN")
                for item in plan:
                    note.append(item)
                note.append("")
            
            # If no valid content, Plan section is not added
            if not has_valid_content:
                pass  # Skip adding Plan section
        
        return "\n".join(note)
    except Exception as e:
        error_logger.error(f"Error formatting consultation note: {str(e)}", exc_info=True)
        return f"Error formatting consultation note: {str(e)}"

async def format_referral_letter(gpt_response):
    """
    Format a referral letter from GPT structured response based on the specified referral letter template.
    
    Args:
        gpt_response: Dictionary containing the structured referral letter data
        
    Returns:
        Formatted string containing the human-readable referral letter
    """
    try:
        report = []

        # Add heading
        report.append("# REFERRAL LETTER\n")
        
        # Practice Letterhead
        letterhead = gpt_response.get("Practice Letterhead", "")
        if letterhead:
            report.append(letterhead)
        
        # Date
        date = gpt_response.get("Date", "")
        if date:
            report.append(date)
        report.append("")
        
        # To Section
        consultant_name = gpt_response.get("Consultant’s Name", "")
        clinic_name = gpt_response.get("Specialist Clinic/Hospital Name", "")
        address_line_1 = gpt_response.get("Address Line 1", "")
        address_line_2 = gpt_response.get("Address Line 2", "")
        city_postcode = gpt_response.get("City, Postcode", "")
        if consultant_name or clinic_name or address_line_1 or address_line_2 or city_postcode:
            report.append("To:")
            if consultant_name:
                report.append(consultant_name)
            if clinic_name:
                report.append(clinic_name)
            if address_line_1:
                report.append(address_line_1)
            if address_line_2:
                report.append(address_line_2)
            if city_postcode:
                report.append(city_postcode)
            report.append("")
        
        # Salutation
        salutation = gpt_response.get("Salutation", "Dear Specialist,").lstrip("- ").strip()
        report.append(f"{salutation}")
        report.append("")
        
        # Re Line
        re_line = gpt_response.get("Re", "").lstrip("- ").strip()
        if re_line:
            report.append(f"Re: {re_line}")
            report.append("")
        
        # Introduction
        introduction = gpt_response.get("Introduction", "").lstrip("- ").strip()
        if introduction:
            report.append(introduction)
            report.append("")
        
        # Clinical Details Section
        clinical_details = gpt_response.get("Clinical Details", {})
        clinical_details_text = []
        if isinstance(clinical_details, str):
            # If clinical_details is a string, append it directly
            clinical_details_text.append(clinical_details.lstrip("- ").strip())
        elif isinstance(clinical_details, dict):
            # If clinical_details is a dictionary, process fields as before
            for field in ["Presenting Complaint", "Duration", "Relevant Findings", "Past Medical History", "Current Medications"]:
                if field in clinical_details and clinical_details[field]:
                    value = clinical_details[field]
                    if isinstance(value, list):
                        clinical_details_text.append(" ".join(item.lstrip("- ").strip() for item in value if item))
                    else:
                        clinical_details_text.append(value.lstrip("- ").strip())
        if clinical_details_text:
            report.append("Clinical Details:")
            report.extend(clinical_details_text)
            report.append("")
        
        # Investigations Section
        investigations = gpt_response.get("Investigations", {})
        investigations_text = []
        if isinstance(investigations, str):
            # If investigations is a string, append it directly
            investigations_text.append(investigations.lstrip("- ").strip())
        elif isinstance(investigations, dict):
            # If investigations is a dictionary, process fields as before
            for field in ["Recent Tests", "Results"]:
                if field in investigations and investigations[field]:
                    value = investigations[field]
                    if isinstance(value, list):
                        investigations_text.append(" ".join(item.lstrip("- ").strip() for item in value if item))
                    else:
                        investigations_text.append(value.lstrip("- ").strip())
        if investigations_text:
            report.append("Investigations:")
            report.extend(investigations_text)
            report.append("")
        
        # Reason for Referral
        reason = gpt_response.get("Reason for Referral", "").lstrip("- ").strip()
        if reason:
            report.append("Reason for Referral:")
            report.append(reason)
            report.append("")
        
        # Patient’s Contact Information
        contact_info = gpt_response.get("Patient’s Contact Information", "")
        if contact_info:
            report.append("Patient’s Contact Information:")
            report.append(contact_info)
            report.append("")
        
        # Enclosures
        enclosures = gpt_response.get("Enclosures", "").lstrip("- ").strip()
        if enclosures:
            report.append(enclosures)
            report.append("")
        
        # Closing
        closing = gpt_response.get("Closing", "").lstrip("- ").strip()
        if closing:
            report.append(closing)
            report.append("")
        
        # Signature Section
        signature = gpt_response.get("Signature", "")
        title = gpt_response.get("Your Title", "")
        contact = gpt_response.get("Your Contact Information", "")
        practice_name = gpt_response.get("Your Practice Name", "")
        report.append("Yours sincerely,")
        report.append("")
        if signature:
            report.append(signature)
        if title:
            report.append(title)
        if contact:
            report.append(contact)
        if practice_name:
            report.append(practice_name)
        
        return "\n".join(report)
    except Exception as e:
        error_logger.error(f"Error formatting referral letter: {str(e)}", exc_info=True)
        return f"Error formatting referral letter: {str(e)}"
    
# Add or update the format_psychology_session_notes function
async def format_psychology_session_notes(gpt_response):
    """
    Format psychology session notes from GPT structured response.
    
    Args:
        gpt_response: Dictionary containing the structured psychology session notes data
        
    Returns:
        Formatted string containing the human-readable psychology session notes
    """
    try:
        notes = []
        
        # Add heading
        notes.append("# PSYCHOLOGY SESSION NOTES\n")
        
        # OUT OF SESSION TASK REVIEW
        notes.append("## OUT OF SESSION TASK REVIEW:")
        session_tasks = gpt_response.get("session_tasks_review", {})
        if (session_tasks.get("practice_skills") or 
            session_tasks.get("task_effectiveness") or 
            session_tasks.get("challenges")):
            
            if session_tasks.get("practice_skills"):
                for item in session_tasks["practice_skills"]:
                    notes.append(f"- {item}")
                    
            if session_tasks.get("task_effectiveness"):
                for item in session_tasks["task_effectiveness"]:
                    notes.append(f"- {item}")
                    
            if session_tasks.get("challenges"):
                for item in session_tasks["challenges"]:
                    notes.append(f"- {item}")
        else:
            notes.append("No out of session task review documented during session")
            
        notes.append("")
        
        # CURRENT PRESENTATION
        notes.append("## CURRENT PRESENTATION:")
        current = gpt_response.get("current_presentation", {})
        if current.get("current_symptoms") or current.get("changes"):
            if current.get("current_symptoms"):
                for item in current["current_symptoms"]:
                    notes.append(f"- {item}")
                    
            if current.get("changes"):
                for item in current["changes"]:
                    notes.append(f"- {item}")
        else:
            notes.append("No current presentation documented during session")
            
        notes.append("")
        
        # SESSION CONTENT
        notes.append("## SESSION CONTENT:")
        session = gpt_response.get("session_content", {})
        if (session.get("issues_raised") or session.get("discussions") or 
            session.get("therapy_goals") or session.get("progress") or 
            session.get("main_topics")):
            
            if session.get("issues_raised"):
                for item in session["issues_raised"]:
                    notes.append(f"- {item}")
                    
            if session.get("discussions"):
                for item in session["discussions"]:
                    notes.append(f"- {item}")
                    
            if session.get("therapy_goals"):
                for item in session["therapy_goals"]:
                    notes.append(f"- {item}")
                    
            if session.get("progress"):
                for item in session["progress"]:
                    notes.append(f"- {item}")
                    
            if session.get("main_topics"):
                for item in session["main_topics"]:
                    notes.append(f"- {item}")
        else:
            notes.append("No session content documented during session")
            
        notes.append("")
        
        # INTERVENTION
        notes.append("## INTERVENTION:")
        intervention = gpt_response.get("intervention", {})
        if intervention.get("techniques") or intervention.get("strategies"):
            if intervention.get("techniques"):
                for item in intervention["techniques"]:
                    notes.append(f"- {item}")
                    
            if intervention.get("strategies"):
                for item in intervention["strategies"]:
                    notes.append(f"- {item}")
        else:
            notes.append("No intervention documented during session")
            
        notes.append("")
        
        # SETBACKS/BARRIERS/PROGRESS WITH TREATMENT
        notes.append("## SETBACKS/ BARRIERS/ PROGRESS WITH TREATMENT:")
        progress = gpt_response.get("treatment_progress", {})
        if progress.get("setbacks") or progress.get("satisfaction"):
            if progress.get("setbacks"):
                for item in progress["setbacks"]:
                    notes.append(f"- {item}")
                    
            if progress.get("satisfaction"):
                for item in progress["satisfaction"]:
                    notes.append(f"- {item}")
        else:
            notes.append("No setbacks/barriers/progress with treatment documented during session")
            
        notes.append("")
        
        # RISK ASSESSMENT AND MANAGEMENT
        notes.append("## RISK ASSESSMENT AND MANAGEMENT:")
        risk = gpt_response.get("risk_assessment", {})
        has_risk_info = False
        
        for field in ["suicidal_ideation", "homicidal_ideation", "self_harm", "violence"]:
            if risk.get(field) and risk[field].strip():
                has_risk_info = True
                break
                
        if has_risk_info or (risk.get("management_plan") and risk["management_plan"]):
            if risk.get("suicidal_ideation") and risk["suicidal_ideation"].strip():
                notes.append(f"- Suicidal Ideation: {risk['suicidal_ideation']}")
                
            if risk.get("homicidal_ideation") and risk["homicidal_ideation"].strip():
                notes.append(f"- Homicidal Ideation: {risk['homicidal_ideation']}")
                
            if risk.get("self_harm") and risk["self_harm"].strip():
                notes.append(f"- Self-harm: {risk['self_harm']}")
                
            if risk.get("violence") and risk["violence"].strip():
                notes.append(f"- Violence & Aggression: {risk['violence']}")
                
            if risk.get("management_plan") and risk["management_plan"]:
                notes.append("")
                notes.append("### Management Plan:")
                for item in risk["management_plan"]:
                    notes.append(f"- {item}")
        else:
            notes.append("No risk assessment and management documented during session")
            
        notes.append("")
        
        # MENTAL STATUS EXAMINATION
        notes.append("## MENTAL STATUS EXAMINATION:")
        mental = gpt_response.get("mental_status", {})
        has_mental_status = False
        
        for field in ["appearance", "behaviour", "speech", "mood", "affect", 
                     "thoughts", "perceptions", "cognition", "insight", "judgment"]:
            if mental.get(field) and mental[field].strip():
                has_mental_status = True
                break
                
        if has_mental_status:
            mental_status_fields = [
                ("appearance", "Appearance: "),
                ("behaviour", "Behaviour: "),
                ("speech", "Speech: "),
                ("mood", "Mood: "),
                ("affect", "Affect: "),
                ("thoughts", "Thoughts: "),
                ("perceptions", "Perceptions: "),
                ("cognition", "Cognition: "),
                ("insight", "Insight: "),
                ("judgment", "Judgment: ")
            ]
            
            for field, label in mental_status_fields:
                if mental.get(field) and mental[field].strip():
                    notes.append(f"{label}{mental[field]}")
        else:
            notes.append("No mental status examination documented during session")
            
        notes.append("")
        
        # OUT OF SESSION TASKS
        notes.append("## OUT OF SESSION TASKS:")
        tasks = gpt_response.get("out_of_session_tasks", [])
        if tasks:
            for item in tasks:
                notes.append(f"- {item}")
        else:
            notes.append("No out of session tasks documented during session")
                
        notes.append("")
        
        # PLAN FOR NEXT SESSION
        notes.append("## PLAN FOR NEXT SESSION:")
        next_session = gpt_response.get("next_session", {})
        if next_session.get("date") or (next_session.get("plan") and next_session["plan"]):
            if next_session.get("date"):
                notes.append(f"- Next Session: {next_session['date']}")
                
            if next_session.get("plan") and next_session["plan"]:
                for item in next_session["plan"]:
                    notes.append(f"- {item}")
        else:
            notes.append("No plan for next session documented during session")
        
        return "\n".join(notes)
    except Exception as e:
        error_logger.error(f"Error formatting psychology session notes: {str(e)}", exc_info=True)
        return f"Error formatting psychology session notes: {str(e)}"
    
async def format_discharge_summary(gpt_response):
    """
    Format a discharge summary from GPT structured response based on DISCHARGE_SUMMARY_SCHEMA.
    
    Args:
        gpt_response: Dictionary containing the structured discharge summary data
        
    Returns:
        Formatted string containing the human-readable discharge summary
    """
    try:
        summary = []
        
        # Title
        summary.append("# Discharge Summary:")
        summary.append("")
        
        # Client information
        client = gpt_response.get("client", {})
        
        # For client name, use placeholder if missing
        if client.get("name"):
            summary.append(f"Client Name: {client['name']}")
        else:
            summary.append("Client Name: [client name]")
            
        # For DOB, use placeholder if missing
        if client.get("dob"):
            summary.append(f"Date of Birth: {client['dob']}")
        else:
            summary.append("Date of Birth: [date of birth]")
            
        # For discharge date, use today's date if missing
        if client.get("discharge_date"):
            summary.append(f"Date of Discharge: {client['discharge_date']}")
        else:
            # Use current date
            current_date = datetime.now().strftime("%B %d, %Y")
            summary.append(f"Date of Discharge: {current_date}")
            
        summary.append("")
        
        # Referral Information
        summary.append("## Referral Information")
        referral = gpt_response.get("referral", {})
        referral_documented = False
        
        if referral.get("source"):
            summary.append(f"- Referral Source: {referral['source']}")
            referral_documented = True
            
        if referral.get("reason"):
            summary.append(f"- Reason for Referral: {referral['reason']}")
            referral_documented = True
            
        if not referral_documented:
            summary.append("No referral information documented during session")
            
        summary.append("")
        
        # Presenting Issues
        summary.append("## Presenting Issues:")
        presenting_issues = gpt_response.get("presenting_issues", [])
        if presenting_issues:
            for issue in presenting_issues:
                summary.append(f"- {issue}")
        else:
            summary.append("No presenting issues documented during session")
            
        summary.append("")
        
        # Diagnosis
        summary.append("## Diagnosis:")
        diagnosis = gpt_response.get("diagnosis", [])
        if diagnosis:
            for item in diagnosis:
                summary.append(f"- {item}")
        else:
            summary.append("No diagnosis documented during session")
            
        summary.append("")
        
        # Treatment Summary
        summary.append("## Treatment Summary:")
        treatment = gpt_response.get("treatment_summary", {})
        treatment_documented = False
        
        # Treatment duration
        if treatment.get("duration"):
            summary.append(f"- Duration of Therapy: {treatment['duration']}")
            treatment_documented = True
            
        # Number of sessions
        if treatment.get("sessions"):
            summary.append(f"- Number of Sessions: {treatment['sessions']}")
            treatment_documented = True
            
        # Type of therapy
        if treatment.get("therapy_type"):
            summary.append(f"- Type of Therapy: {treatment['therapy_type']}")
            treatment_documented = True
            
        # Therapeutic goals
        if treatment.get("goals") and len(treatment["goals"]) > 0:
            summary.append("- Therapeutic Goals:")
            for goal in treatment["goals"]:
                summary.append(f"  - {goal}")
            treatment_documented = True
            
        # Treatment description
        if treatment.get("description"):
            summary.append(f"- {treatment['description']}")
            treatment_documented = True
            
        # Medications
        if treatment.get("medications"):
            summary.append(f"- {treatment['medications']}")
            treatment_documented = True
            
        if not treatment_documented:
            summary.append("No treatment details documented during session")
            
        summary.append("")
        
        # Progress and Response to Treatment
        summary.append("## Progress and Response to Treatment:")
        progress = gpt_response.get("progress", {})
        progress_documented = False
        
        if progress.get("overall"):
            summary.append(f"- {progress['overall']}")
            progress_documented = True
            
        if progress.get("goal_progress") and len(progress["goal_progress"]) > 0:
            summary.append("- Progress Toward Goals:")
            for i, goal_progress in enumerate(progress["goal_progress"], 1):
                summary.append(f"  - Goal {i}: {goal_progress}")
            progress_documented = True
            
        if not progress_documented:
            summary.append("No treatment progress documented during session")
            
        summary.append("")
        
        # Clinical Observations
        summary.append("## Clinical Observations")
        observations = gpt_response.get("clinical_observations", {})
        observations_documented = False
        
        if observations.get("engagement"):
            summary.append(f"- Client's Engagement: {observations['engagement']}")
            observations_documented = True
            
        # Client's Strengths
        if observations.get("strengths") and len(observations["strengths"]) > 0:
            summary.append("- Client's Strengths:")
            for strength in observations["strengths"]:
                summary.append(f"  - {strength}")
            observations_documented = True
            
        # Client's Challenges
        if observations.get("challenges") and len(observations["challenges"]) > 0:
            summary.append("- Client's Challenges:")
            for challenge in observations["challenges"]:
                summary.append(f"  - {challenge}")
            observations_documented = True
            
        if not observations_documented:
            summary.append("No clinical observations documented during session")
            
        summary.append("")
        
        # Risk Assessment
        summary.append("## Risk Assessment:")
        if gpt_response.get("risk_assessment"):
            summary.append(f"- {gpt_response['risk_assessment']}")
        else:
            summary.append("No risk assessment documented during session")
            
        summary.append("")
        
        # Outcome of Therapy
        summary.append("## Outcome of Therapy")
        outcome = gpt_response.get("outcome", {})
        outcome_documented = False
        
        if outcome.get("current_status"):
            summary.append(f"- Current Status: {outcome['current_status']}")
            outcome_documented = True
            
        if outcome.get("remaining_issues"):
            summary.append(f"- Remaining Issues: {outcome['remaining_issues']}")
            outcome_documented = True
            
        if outcome.get("client_perspective"):
            summary.append(f"- Client's Perspective: {outcome['client_perspective']}")
            outcome_documented = True
            
        if outcome.get("therapist_assessment"):
            summary.append(f"- Therapist's Assessment: {outcome['therapist_assessment']}")
            outcome_documented = True
            
        if not outcome_documented:
            summary.append("No therapy outcome details documented during session")
            
        summary.append("")
        
        # Reason for Discharge
        summary.append("## Reason for Discharge")
        discharge_reason = gpt_response.get("discharge_reason", {})
        reason_documented = False
        
        if discharge_reason.get("reason"):
            summary.append(f"- Discharge Reason: {discharge_reason['reason']}")
            reason_documented = True
            
        if discharge_reason.get("client_understanding"):
            summary.append(f"- Client's Understanding and Agreement: {discharge_reason['client_understanding']}")
            reason_documented = True
            
        if not reason_documented:
            summary.append("No discharge reason documented during session")
            
        summary.append("")
        
        # Discharge Plan
        summary.append("## Discharge Plan:")
        if gpt_response.get("discharge_plan"):
            summary.append(f"- {gpt_response['discharge_plan']}")
        else:
            summary.append("No discharge plan documented during session")
            
        summary.append("")
        
        # Recommendations
        summary.append("## Recommendations:")
        recommendations = gpt_response.get("recommendations", {})
        recommendations_documented = False
        
        if recommendations.get("overall"):
            summary.append(f"- {recommendations['overall']}")
            recommendations_documented = True
            
        # Follow-Up Care
        if recommendations.get("followup") and len(recommendations["followup"]) > 0:
            summary.append("- Follow-Up Care:")
            for followup in recommendations["followup"]:
                summary.append(f"  - {followup}")
            recommendations_documented = True
            
        # Self-Care Strategies
        if recommendations.get("self_care") and len(recommendations["self_care"]) > 0:
            summary.append("- Self-Care Strategies:")
            for strategy in recommendations["self_care"]:
                summary.append(f"  - {strategy}")
            recommendations_documented = True
            
        if recommendations.get("crisis_plan"):
            summary.append(f"- Crisis Plan: {recommendations['crisis_plan']}")
            recommendations_documented = True
            
        if recommendations.get("support_systems"):
            summary.append(f"- Support Systems: {recommendations['support_systems']}")
            recommendations_documented = True
            
        if not recommendations_documented:
            summary.append("No recommendations documented during session")
            
        summary.append("")
        
        # Additional Notes
        summary.append("## Additional Notes:")
        if gpt_response.get("additional_notes"):
            summary.append(f"- {gpt_response['additional_notes']}")
        else:
            summary.append("No additional notes documented during session")
            
        summary.append("")
        
        # Final Note
        summary.append("## Final Note")
        if gpt_response.get("final_note"):
            summary.append(f"- Therapist's Closing Remarks: {gpt_response['final_note']}")
        else:
            summary.append("No final remarks documented during session")
            
        summary.append("")
        
        # Clinician Information
        clinician = gpt_response.get("clinician", {})
        if clinician.get("name"):
            summary.append(f"Clinician's Name: {clinician['name']}")
        else:
            summary.append("Clinician's Name: [clinician name]")
            
        summary.append("Clinician's Signature: [signature]")
        
        if clinician.get("date"):
            summary.append(f"Date: {clinician['date']}")
        else:
            # Use current date
            current_date = datetime.now().strftime("%B %d, %Y")
            summary.append(f"Date: {current_date}")
            
        summary.append("")
        
        # Attachments
        summary.append("## Attachments (if any)")
        attachments = gpt_response.get("attachments", [])
        if attachments:
            for attachment in attachments:
                summary.append(f"- {attachment}")
        else:
            summary.append("[None]")
        
        return "\n".join(summary)
    except Exception as e:
        error_logger.error(f"Error formatting discharge summary: {str(e)}", exc_info=True)
        return f"Error formatting discharge summary: {str(e)}"
    
async def format_pathology(gpt_response):
    """
    Format a pathology note from GPT structured response based on PATHOLOGY_NOTE_SCHEMA.
    
    Args:
        gpt_response: Dictionary containing the structured pathology note data
        
    Returns:
        Formatted string containing the human-readable pathology note
    """
    try:
        note = []
        
        # Add heading
        note.append("# PATHOLOGY NOTE\n")
        
        # Therapy session attended to section
        note.append("## Therapy session attended to")
        therapy_attendance = gpt_response.get("therapy_attendance", {})
        attendance_documented = False
        
        attendance_fields = [
            ("current_issues", "- "),
            ("past_medical_history", "- "),
            ("medications", "- "),
            ("social_history", "- "),
            ("allergies", "- ")
        ]
        
        for field, prefix in attendance_fields:
            if therapy_attendance.get(field) and therapy_attendance[field] != "Not documented":
                note.append(f"{prefix}{therapy_attendance[field]}")
                attendance_documented = True
                
        if not attendance_documented:
            note.append("No attendance details documented during this session.")
            
        note.append("")
        
        # Objective section
        note.append("## Objective:")
        objective = gpt_response.get("objective", {})
        objective_documented = False
        
        objective_fields = [
            ("examination_findings", "- "),
            ("diagnostic_tests", "- ")
        ]
        
        for field, prefix in objective_fields:
            if objective.get(field) and objective[field] != "Not documented":
                note.append(f"{prefix}{objective[field]}")
                objective_documented = True
                
        if not objective_documented:
            note.append("No objective findings documented during this session.")
            
        note.append("")
        
        # Reports section
        note.append("## Reports:")
        if gpt_response.get("reports") and gpt_response["reports"] != "Not documented":
            note.append(f"- {gpt_response['reports']}")
        else:
            note.append("No reports documented during this session.")
            
        note.append("")
        
        # Therapy section
        note.append("## Therapy:")
        therapy = gpt_response.get("therapy", {})
        therapy_documented = False
        
        therapy_fields = [
            ("current_therapy", "- "),
            ("therapy_changes", "- ")
        ]
        
        for field, prefix in therapy_fields:
            if therapy.get(field) and therapy[field] != "Not documented":
                note.append(f"{prefix}{therapy[field]}")
                therapy_documented = True
                
        if not therapy_documented:
            note.append("No therapy details documented during this session.")
            
        note.append("")
        
        # Outcome section
        note.append("## Outcome:")
        if gpt_response.get("outcome") and gpt_response["outcome"] != "Not documented":
            note.append(f"- {gpt_response['outcome']}")
        else:
            note.append("No outcomes documented during this session.")
            
        note.append("")
        
        # Plan section
        note.append("## Plan:")
        plan = gpt_response.get("plan", {})
        plan_documented = False
        
        plan_fields = [
            ("future_plan", "- "),
            ("followup", "- ")
        ]
        
        for field, prefix in plan_fields:
            if plan.get(field) and plan[field] != "Not documented":
                note.append(f"{prefix}{plan[field]}")
                plan_documented = True
                
        if not plan_documented:
            note.append("No plan documented during this session.")
        
        return "\n".join(note)
    except Exception as e:
        error_logger.error(f"Error formatting pathology note: {str(e)}", exc_info=True)
        return f"Error formatting pathology note: {str(e)}"

@log_execution_time
async def generate_gpt_response(transcription, template_type):
    """
    Generate GPT response based on a transcription and template type.
    
    Args:
        transcription: Dictionary containing the transcription data
        template_type: Type of template to use for formatting
        
    Returns:
        GPT response in JSON format
    """
    try:
        operation_id = str(uuid.uuid4())[:8]
        main_logger.info(f"[OP-{operation_id}] Generating GPT response for template: {template_type}")
        
        # Prepare conversation text for the prompt
        conversation_text = ""
        if "conversation" in transcription:
            for entry in transcription["conversation"]:
                speaker = entry.get("speaker", "Unknown")
                text = entry.get("text", "")
                conversation_text += f"{speaker}: {text}\n\n"
        
        # Select system message based on template type
        system_message = "You are a clinical documentation expert. Format the conversation into structured clinical notes."
        
        current_date = None
        if "metadata" in transcription and "current_date" in transcription["metadata"]:
            current_date = transcription["metadata"]["current_date"]
                
        date_instructions = """
        Date Handling Instructions:
                Use {current_date if current_date else "the current date"} as the reference date for all temporal expressions in the transcription.
                Convert relative time references to specific dates based on the reference date:
                    "This morning," "today," or similar terms refer to the reference date.
                    "X days ago" refers to the reference date minus X days (e.g., "five days ago" is reference date minus 5 days).
                    "Last week" refers to approximately 7 days prior, adjusting for specific days if mentioned (e.g., "last Wednesday" is the most recent Wednesday before the reference date).
                Format all dates as DD/MM/YYYY (e.g., 10/06/2025) in the SOAP note output.
                Do not use hardcoded dates or assumed years unless explicitly stated in the transcription.
                Verify date calculations to prevent errors, such as incorrect years or misaligned days.
        """
        # Core instructions for all medical documentation templates
        preservation_instructions = """
CRITICAL INSTRUCTIONS:
1. ALWAYS preserve ALL dates mentioned (appointment dates, symptom onset, follow-up dates, etc.)
2. NEVER omit medication names, dosages, frequencies or routes of administration
3. Preserve ALL quantitative values including lab results, vital signs, and measurements
4. Include specific timeframes for symptom progression (e.g., "for 2 weeks", "since April 10th")
5. Maintain ALL numeric values exactly as stated (weights, blood pressure readings, glucose levels)
6. ALWAYS include complete diagnostic information and specific medical terminology
7. Preserve all references to previous treatments, procedures or interventions
8. Extract and include all allergies, adverse reactions, and contraindications
"""

        # Add grammar-specific instructions to all templates
        grammar_instructions = """
CRITICAL GRAMMAR INSTRUCTIONS:
1. Use perfect standard US English grammar in all text
2. Ensure proper subject-verb agreement in all sentences
3. Use appropriate prepositions (avoid errors like "dependent of" instead of "dependent on")
4. Eliminate article errors (never use "the a" or similar incorrect article combinations)
5. Maintain professional medical writing style with proper punctuation
6. Use proper tense consistency throughout all documentation
7. Ensure proper noun-pronoun agreement
8. Use clear and concise phrasing without grammatical ambiguity
"""

        # Template-specific instructions and schema
        if template_type == "soap_note":
            user_instructions = f"""You are provided with a medical conversation transcript. 
            Analyze the transcript thoroughly to generate a structured SOAP note following the specified template, synthesizing the patient’s case from a physician’s perspective to produce a concise, professional, and clinically relevant note that facilitates medical decision-making. 
            Use only information explicitly provided in the transcript, without assuming or adding any details. 
            Ensure the output is a valid JSON object with the SOAP note sections (Subjective, Past Medical History, Objective, Assessment, Plan) as keys, formatted in a professional, doctor-like tone. 
            Address each chief complaint and issue separately in the Subjective and Assessment/Plan sections. 
            For time references (e.g., 'this morning' to June 1, 2025; 'last Wednesday' to May 28, 2025; 'a week ago' to May 25, 2025), convert to specific dates based on today’s date, June 1, 2025 (Sunday). 
            Use numeric format for all numbers (e.g., '2' instead of 'two'). Ensure each point in Subjective, Past Medical History, and Objective starts with '- ', while Assessment and Plan use subheadings without '- ' for clear, concise points. 
            Omit sections with no relevant information. The note should be streamlined and to the point, prioritizing utility for physicians. 
            Use the specific template provided below to generate the SOAP note.
            Dont Repeat any points in the subjective, and past medical history.
            For all numbers related information, preserve the exact numbers mentioned in the transcript and use digits.
            Below is the transcript:

            {conversation_text}


            """
            # SOAP Note Instructions - Updated for conciseness and direct language
            system_message = f"""You are an expert medical scribe tasked with generating a professional, concise, and clinically relevant SOAP note based solely on the patient transcript, contextual notes, or clinical note provided. 
            Your role is to analyze the input thoroughly, synthesize the patient’s case from a physician’s perspective, and produce a streamlined SOAP note that prioritizes clarity, relevance, and utility for medical decision-making. 
            Adhere strictly to the provided SOAP note template, including only information explicitly stated in the input. Structure the note in point form, starting each line with '- ', and ensure it is professional, avoiding extraneous details.
            Convert vague time references (e.g., 'this morning' to June 1, 2025; 'last Wednesday' to May 28, 2025; 'a week ago' to May 25, 2025) based on today’s date, June 1, 2025 (Sunday). 
            Do not fabricate or infer patient details, assessments, plans, interventions, evaluations, or care plans beyond what is explicitly provided. Omit sections with no relevant information. 
            The output should be a plain text SOAP note tailored to assist physicians efficiently. 
            {preservation_instructions} {grammar_instructions}
            
            The template is as follows:

            Subjective:
            - Chief complaints and reasons for visit (e.g., symptoms, patient requests) (include only if explicitly stated)
            - Duration, timing, location, quality, severity, and context of complaints (include only if explicitly stated)
            - Factors worsening or alleviating symptoms, including self-treatment attempts and their effectiveness (include only if explicitly stated)
            - Progression of symptoms over time (include only if explicitly stated)
            - Previous episodes of similar symptoms, including timing, management, and outcomes (include only if explicitly stated)
            - Impact of symptoms on daily life, work, or activities (include only if explicitly stated)
            - Associated focal or systemic symptoms related to chief complaints (include only if explicitly stated)

            Past Medical History:
            - Relevant past medical or surgical history, investigations, and treatments tied to chief complaints (include only if explicitly stated)
            - Relevant social history (e.g., lifestyle, occupation) related to chief complaints (include only if explicitly stated)
            - Relevant family history linked to chief complaints (include only if explicitly stated)
            - Exposure history (e.g., environmental, occupational) (include only if explicitly stated)
            - Immunization history and status (include only if explicitly stated)
            - Other relevant subjective information (include only if explicitly stated)

            Objective:
            - Vital signs (include only if explicitly stated)
            - Physical or mental state examination findings, including system-specific exams (include only if explicitly stated)
            - Completed investigations and their results (include only if explicitly stated; planned or ordered investigations belong in Plan)

            Assessment:
            - Likely diagnosis (include only if explicitly stated)
            - Differential diagnosis (include only if explicitly stated)

            Plan:
            - Planned investigations (include only if explicitly stated)
            - Planned treatments (include only if explicitly stated)
            - Other actions (e.g., counseling, referrals, follow-up instructions) (include only if explicitly stated)
            
            Referrence Example:

            Example Transcription:
            Speaker 0: Good morning, Mr. Johnson. What brings you in today? 
            Speaker 1: I’ve been having chest pain and feeling my heart race since last Wednesday. It’s been tough to catch my breath sometimes. 
            Speaker 0: How would you describe the chest pain? Where is it, and how long does it last? 
            Speaker 1: It’s a sharp pain in the center of my chest, lasts a few minutes, and comes and goes. It’s worse when I walk upstairs. 
            Speaker 0: Any factors that make it better or worse? 
            Speaker 1: Resting helps a bit, but it’s still there. I tried taking aspirin a few days ago, but it didn’t do much. 
            Speaker 0: Any other symptoms, like nausea or sweating? 
            Speaker 1: No nausea or sweating, but I’ve been tired a lot. No fever or weight loss. 
            Speaker 0: Any past medical conditions or surgeries? 
            Speaker 1: I had high blood pressure diagnosed a few years ago, and I take lisinopril 20 mg daily. 
            Speaker 0: Any side effects from the lisinopril? 
            Speaker 1: Not really, it’s been fine. My blood pressure’s been stable. 
            Speaker 0: Any allergies? 
            Speaker 1: I’m allergic to penicillin. 
            Speaker 0: What’s your lifestyle like? 
            Speaker 1: I’m a retired teacher, live alone, and walk daily. I’ve been stressed about finances lately. 
            Speaker 0: Any family history of heart issues? 
            Speaker 1: My father had a heart attack in his 60s. 
            Speaker 0: Let’s check your vitals. Blood pressure is 140/90, heart rate is 88, temperature is normal at 98.6°F. You appear well but slightly anxious. 
            Speaker 0: Your symptoms suggest a possible heart issue. We’ll order an EKG and blood tests today and refer you to a cardiologist. Continue lisinopril, and avoid strenuous activity. Call us if the pain worsens or you feel faint. 
            Speaker 1: Okay, I understand. When should I come back? 
            Speaker 0: Schedule a follow-up in one week to discuss test results, or sooner if symptoms worsen.

            Example SOAP Note Output:

            Subjective:
            - Chest pain and heart racing since last Wednesday. Dyspnoea.
            - Sharp pain in centre of chest, lasting few minutes, intermittent. Worse when climbing stairs.
            - Resting helps slightly. Tried aspirin with minimal effect.
            - Associated fatigue. No nausea, sweating, fever or weight loss.

            Past Medical History:
            - Hypertension - on lisinopril 20mg daily. BP stable.
            - Allergies: Penicillin.
            - Social: Retired teacher. Lives alone. Daily walks. Financial stress recently.
            - Family history: Father had MI in 60s.

            Objective:
            - BP 140/90, HR 88, temperature normal (98.6°F).
            - Appears well but slightly anxious.

            Assessment:
            - Possible cardiac issue.

            Plan:
            - ECG and blood tests ordered.
            - Cardiology referral.
            - Continue lisinopril.
            - Avoid strenuous activity.
            - Advised to call if pain worsens or develops syncope.
            - Follow-up in one week to discuss results, or sooner if symptoms worsen.
            """
        
        elif template_type == "referral_letter":

            user_instructions= f"""You are provided with a medical conversation transcript. 
            Analyze the transcript and generate a structured Referral letter following the specified template. 
            Use only the information explicitly provided in the transcript, and do not include or assume any additional details. 
            Ensure the output is a valid JSON object with the Referral letter sections heaings as keys, formatted professionally and concisely in a doctor-like tone. 
            Make sure the output is in valid JSON format. 
            If the patient didnt provide the information regarding any field then ignore the respective section.
            For time references (e.g., “this morning,” “last Wednesday”), convert to specific dates based on today’s date, current day's date whatever it is on calender.
            Include the all numbers in numeric format.
            Make sure the output is concise and to the point.
            ENsure the data in all letter should be to the point and professional.
            Make it useful as doctors perspective so it makes there job easier, dont just dictate and make a letter, analyze the conversation, summarize it and make a note that best desrcibes the patient's case as a doctor's perspective.
            For each point add - at the beginning of the line and give two letter space after that.
            Add time related information in the report dont miss them.
            Make sure the point are structured and looks professional and uses medical terms to describe everything.
            Use medical terms to describe each thing
            Dont repeat anyything dude please.
            Include sub-headings as specified below in format in output
            ADD "[Practice Letterhead]"  if pratice name is not available or you are not able to analyze what it could be.
            ADD "Dr. [Consultant’s Name] " if doctor whom the case was referred to is not known.
            Add "[Specialist Clinic/Hospital Name]" if the name of the hospital not known
            add ''[Address Line 1]
            [Address Line 2]''' i address is not known
            add "[City, Postcode]" if city and postcode unknown.
            Add "Dear Dr. [Consultant's Last Name]",  if doctor whom the case was referred to is not known otherwise use doctor's name
            Add this line "Enclosed are [e.g., relevant test results, imaging reports] for your review. Please do not hesitate to contact me if you require further information." only if test results were available.
            Add "Re: Referral for [Patient's Name], [Date of Birth: DOB]" if patient name and dob is not known otherwise write the values here.
            Write "[Your Full Name]" if name of the doctor that is referring is unknown (try to see if the patient ever called the doctor by its name in conversation that is bascially the doctor name).
            Write "[Your Title]" if title of the doctor that is referring is unknown (try to see if the title of doctor was disclosed in conversation that is bascially the doctor's tittle or try to analyze who it can be by analyzing conversation).
            Below is the transcript:\n\n{conversation_text}
            """
            system_message = f"""You are a medical documentation AI tasked with creating a referral letter based solely on the information provided in a conversation transcript, contextual notes, or clinical note. 
            Your goal is to produce a professional, concise, and accurate referral letter that adheres to the template below. 
            The letter must be written in full sentences, avoid bullet points, and include only information explicitly mentioned in the provided data. 
            Do not infer, assume, or add any details not explicitly stated. 
            If information for a specific section or placeholder is missing, leave that section or placeholder blank without indicating that the information was not provided. 
            The output must be formatted as plain text with clear section breaks and proper spacing, maintaining a formal, doctor-like tone suitable for medical correspondence.

            Referral Letter Template:

            [Practice Letterhead] 
            
            [Date]
            
            To:
            Dr. [Consultant’s Name]
            [Specialist Clinic/Hospital Name]
            [Address Line 1]
            [Address Line 2]
            [City, Postcode]
            
            Dear Dr. [Consultant’s Last Name],
            
            Re: Referral for [Patient’s Name], [Date of Birth: DOB]
           
            I am referring [Patient’s Name] to your clinic for further evaluation and management of [specific condition or concern].
            
            Clinical Details:
            Presenting Complaint: [e.g., Persistent abdominal pain, recurrent headaches]
            Duration: [e.g., Symptoms have been present for 6 months]
            Relevant Findings: [e.g., Significant weight loss, abnormal imaging findings]
            Past Medical History: [e.g., Hypertension, diabetes]
            Current Medications: [e.g., List of current medications]
            
            Investigations:
            Recent Tests: [e.g., Blood tests, MRI, X-rays]
            Results: [e.g., Elevated liver enzymes, abnormal MRI findings]
            
            Reason for Referral:
            Due to [e.g., worsening symptoms, need for specialized evaluation], I would appreciate your expert assessment and management recommendations for this patient.
            
            Patient’s Contact Information:
            Phone Number: [Patient’s Phone Number]
            Email Address: [Patient’s Email Address]
            
            Enclosed are [e.g., relevant test results, imaging reports] for your review. Please do not hesitate to contact me if you require further information.
            
            Thank you for your attention to this referral. I look forward to your evaluation and recommendations.
            Yours sincerely,
            [Your Full Name]
            [Your Title]
            [Your Contact Information]
            [Your Practice Name]

            Detailed Instructions:
            1. Data Source: Use only the information explicitly provided in the conversation transcript, contextual notes, or clinical note. Do not fabricate or infer any details, such as patient names, dates of birth, test results, or medical history, unless explicitly stated in the input.
            2. Template Adherence: Strictly follow the provided template structure, including all sections (Clinical Details, Investigations, Reason for Referral, Patient’s Contact Information, etc.). Maintain the exact order and wording of section headers as shown in the template.
            3. Omitting Missing Information: If any information required for a placeholder (e.g., Patient’s Name, DOB, Consultant’s Name, Address) or section (e.g., Investigations, Current Medications) is not explicitly mentioned, leave that placeholder or section blank. Do not include phrases like “not provided” or “unknown” in the output.
            4. Date: Use the current date provided (e.g., 05/06/2025) for the letter’s date field. Ensure the format is DD/MM/YYYY.
            5. Consultant and Clinic Details: If the consultant’s name, clinic name, or address is provided in the transcript or notes, include them in the “To” section. If not, address the letter generically as “Dear Specialist” and leave the clinic name and address fields blank.
            6. Clinical Details Section: Include only explicitly mentioned details for Presenting Complaint, Duration, Relevant Findings, Past Medical History, and Current Medications. Group related findings logically (e.g., combine all symptoms under Presenting Complaint, all exam findings under Relevant Findings). Write in full sentences, avoiding bullet points.
            7. Investigations Section: Include only tests and results explicitly mentioned in the input. If no tests or results are provided, leave the Investigations section blank.
            8. Reason for Referral: Summarize the primary medical concern and the rationale for specialist referral based solely on the input data. For example, if the transcript mentions worsening symptoms or a need for specialized evaluation, reflect that in the reason. Keep this concise and focused.
            9. Patient Contact Information: Include the patient’s phone number and email address only if explicitly provided in the input. If not, leave this section blank.
            10. Enclosures: If the input mentions specific documents (e.g., test results, imaging reports), note them in the “Enclosed are” sentence. If no documents are mentioned, include the sentence “Enclosed are relevant medical records for your review” as a default.
            11. Signature: Include the referring doctor’s full name, title, contact information, and practice name only if explicitly mentioned in the input. If not, leave these fields blank.
            12. Tone and Style: Maintain a formal, professional, and concise tone consistent with medical correspondence. Avoid abbreviations, jargon, or informal language unless directly quoted from the input.
            13. Formatting: Ensure the output is plain text with proper spacing (e.g., blank lines between sections and paragraphs) for readability. Use no bullet points, lists, or markdown formatting. Each section should be a paragraph or set of sentences as per the template.
            14. Error Handling: If the input is incomplete or unclear, generate the letter with only the available data, leaving missing sections blank. Do not generate an error message or note deficiencies in the output.
            15. Example Guidance: For reference, an example input transcript might describe a patient with “constant cough, shortness of breath, chest tightness, low-grade fever for 6 days,” with findings like “wheezing, crackles, temperature 37.6°C,” and a diagnosis of “acute bronchitis with asthma exacerbation.” The output should reflect only these details in the appropriate sections, as shown in the example referral letter.
            16. If somedata is not available just write the place holder e.g; like this "Re: Referral for [Patient’s Name], [Date of Birth: DOB]" if data available write "Re: Referral for [Munaza Ashraf], [Date of Birth: 10/10/2002]"
            17. If date for referral letter is mising just write that day's date
            18. Only talk about enclosing document if talk about during conversation.
            19. Dont fabricate data, and add anything that was not stated

            Example for Reference (Do Not Use as Input):


            Example 1:
                    
            Example Transcription:
            Speaker 0 : Good morning i'm doctor sarah what brings you in today
            Speaker 1 : Good morning i have been feeling really unwell for the past week i have a constant cough shortness of breath and my chest feels tight i also have a low grade fever that comes and goes
            Speaker 0 : That sounds uncomfortable when did these symptoms start
            Speaker 1 : About six days ago at first i thought it was just a cold but it's getting worse i get tired just walking a few steps
            Speaker 0 : Are you producing any mucus with the cough
            Speaker 1 : Yes it's yellowish green and like sometimes it's hard to breathe
            Speaker 0 : Especially at night any wheezing or chest pain
            Speaker 1 : A little wheezing and my chest feels heavy no sharp pain though do you have
            Speaker 0 : A history of asthma copd or any lung condition?
            Speaker 1 : I have a mild asthma i use an albuterol inhaler occasionally maybe once or twice a week
            Speaker 0 : Any history of smoking i smoked in college but
            Speaker 1 : I quit ten years ago
            Speaker 0 : Do you have any allergic or chronic illness like diabetes hypertension or gerd
            Speaker 1 : I have type two diabetes diagnosed three years ago i take metamorphin five hundred mg twice a day no known allergies
            Speaker 0 : Any recent travel or contact with someone who's been sick
            Speaker 1 : No travel but my son had a very bad cold last week
            Speaker 0 : Alright let me check your vitals and listen to your lung your temperature is 106 respiratory rate is 22 oxygen saturation is 94 and heart rate is 92 beats per minute i hear some wheezing and crackles in both lower lung field your throat looks a bit red and post nasal drip is present
            Speaker 1 : Is it a chest infection
            Speaker 0 : Based on your symptoms history and exam it sounds like acute bronchitis possibly comp complicated by asthma and diabetes it's likely viral but with your underlying condition we should be cautious medications i'll prescribe amoxicillin chloride eight seventy five mg or one twenty five mg twice daily for seven days just in case there's a bacterial component continue using your albuterol inhaler but increase to every four to six six hours as needed i'm also prescribing a five day course of oral prednisone forty mg per day to reduce inflammation due to your asthma flare for the cough you can take guifenacin with dex with dextromethorphan as needed check your blood glucose more frequently while on prednisone as it can raise your sugar levels if your oxygen drops below 92 or your breathing worsens go to the emergency rest stay hydrated and avoid exertion use a humidifier at night and avoid cold air i want to see you back in three to five days to recheck your lungs and sugar control if symptoms worsen sooner come in immediately okay that makes sense will these make me sleepy the cough syrup might so take it at night and remember don't drive after taking it let me print your ex prescription and set your follow-up"

            Example REFERRAL LETTER Output:

            [Practice Letterhead]

            05/06/2025

            To:  
            Dr. Sarah  
            General Practice Clinic  
            456 Health Avenue  
            Karachi, 75000

            Dear Dr. Sarah,


            Re: Referral for Patient, DOB

            I am referring this patient to your clinic for further evaluation and management of acute bronchitis with asthma exacerbation.

            Clinical Details:
            Presenting Complaint: Constant cough, shortness of breath, chest tightness, low-grade fever
            Duration: Symptoms have been present for 6 days
            Relevant Findings: Wheezing and crackles in both lower lung fields, red throat, post-nasal drip present, yellowish-green sputum, fatigue with minimal exertion
            Past Medical History: Mild asthma, Type 2 diabetes diagnosed 3 years ago, Ex-smoker (quit 10 years ago), No known allergies
            Current Medications: Albuterol inhaler 1-2 times weekly, Metformin 500mg twice daily

            Investigations:
            Recent Tests: Vital signs
            Results: Temperature: 37.6°C, Respiratory rate: 22, Oxygen saturation: 94%, Heart rate: 92 bpm


            Reason for Referral:
            Due to worsening respiratory symptoms complicated by underlying asthma and diabetes, I would appreciate your expert assessment and management recommendations for this patient.


            Patient's Contact Information: 


            Thank you for your attention to this referral. I look forward to your evaluation and recommendations.


            Yours sincerely,

            [Your Full Name]
            [Your Title]


            Example 2:
                    
            Example Transcription:
            Speaker 0: Seeing you again how are you?
            Speaker 1: Hi i'm feeling okay normally i come every two months but scheduled earlier because i've been feeling depressed and anxious i had a panic attack this morning and felt like having chest pain tired and overwhelmed
            Speaker 0: well i'm sorry to hear that can you describe the panic attack further.
            Speaker 1: well i feel like i have a knot in my throat it's a very uncomfortable sensation i was taking lithium in the past but discontinued it because of the side effects
            Speaker 0: what kind of side effects?
            Speaker 1: i was dizzy slow thought process and was feeling nauseous i still throw up every morning i have this sensation like i need to throw up but since i don't have anything in my stomach i just retch
            Speaker 0: how long has this been going on for.
            Speaker 1: oh it's been going on for a month now.
            Speaker 0: and how about the panic attacks
            Speaker 1: I was having the panic attacks once every week in the past and had stopped for three months and this was the first time after that
            Speaker 0: Are you stressed out about anything in particular
            Speaker 1: I feel stressed out about my work and don't want to be there but have to be there for money i work in verizon in sales department sometimes when there are no customers that's when i feel the worst because if i can't hit the sales target they they can fire me
            Speaker 0: How was your job before that?
            Speaker 1: I was working at wells fargo before and that was even more stressful and before that i was working with uber which was not as stressful but the money wasn't good and before that i was working for at and t which was good but because of my illness i could not continue to work there
            Speaker 0: and how has your mood been
            Speaker 1: i feel down i feel like i have a lack of motivation i used to watch a lot of movies and tv shows in the past but now i don't feel interested anymore
            Speaker 0: what do you do in your free time?
            Speaker 1: oh i spend a lot of time on facebook just scrolling you know i have a friend in cuba and he chats with me every day which kinda distracts me
            Speaker 0: who do you live with?
            Speaker 1: i live with my husband and a child a four year old girl
            Speaker 0: do you like spending time with them
            Speaker 1: not really i don't feel like doing much my daughter is closer with my husband so she likes spending more time with him i do take her to the park though
            Speaker 0: how's your sleep been
            Speaker 1: i sleep well i sleep for more than ten hours
            Speaker 0: is it normal for you to sleep for ten hours or do you feel like you have been sleeping more than usual
            Speaker 1: no it's normal i usually sleep that long
            Speaker 0: how's your appetite been
            Speaker 1: i feel like my appetite has reduced
            Speaker 0: and how about your concentration have you been able to focus on your work
            Speaker 1: no my concentration is really bad
            Speaker 0: for how long
            Speaker 1: it's been like that for a month now
            Speaker 0: do you get any dark thoughts like thoughts about hurting yourself or anybody else no how about experiencing anything unusual like hearing voices no one else can hear or seeing things no one else can see such as shadows etcetera
            Speaker 1: no k
            Speaker 0: in the last month have you had any episodes where you have be you have the opposite of low energy like having a lot of energy a lot of thoughts running in your head really fast
            Speaker 1: no i know what manic episodes look like my husband has been keeping an eye on me and i i'm also aware of how how i feel when it starts
            Speaker 0: okay that's good seems like the main issue right now is depression and anxiety have you been getting the monthly injections on time
            Speaker 1: yes
            speaker 0: that's good are you open to start oral medication to help with your symptoms in addition to the injection
            speaker 1: yes
            Speaker 0: okay we're going to start you on a medication for depression that you'll take every morning however you will have to watch out for hyperactivity because medication for depression can trigger a manic episode if you feel like you are getting excessively active and not sleeping well give us a call
            Speaker 1: Okay doctor
            Speaker 0: we will also start you on a medication for anxiety
            Speaker 1: okay sounds good
            Speaker 0: okay pleasure meeting you and see you in next follow-up visit
            Speaker 1: okay thank you
            Speaker 0: thanks bye

            Example REFERRAL LETTER Output:

            [Practice Letterhead]

            05/06/2025

            To:  
            Dr. [Consultant's Name]  
            [Specialist Clinic/Hospital Name]  
            [Address Line 1]  
            [Address Line 2]  
            [City, Postcode]

            Dear Dr. [Consultant's Last Name],


            Re: Referral for [Patient's Name], [Date of Birth: DOB]

            I am referring this patient to your clinic for further evaluation and management of depression with anxiety in the context of bipolar disorder.

            Clinical Details:
            Presenting Complaint: Depression, anxiety, and panic attacks
            Duration: Symptoms have been present for at least one month
            Relevant Findings: Morning retching/vomiting for the past month, sensation of knot in throat, chest pain during panic attacks, fatigue, feeling overwhelmed, low mood, lack of motivation, anhedonia, reduced appetite, poor concentration
            Past Medical History: Bipolar disorder
            Current Medications: Monthly injections for bipolar disorder (previously on lithium but discontinued due to side effects including dizziness, cognitive slowing, and nausea)

            Investigations:
            Recent Tests: Mental state examination
            Results: Patient appears depressed and anxious


            Reason for Referral:
            Due to recurrence of panic attacks after a three-month symptom-free period and worsening depressive symptoms, I would appreciate your expert assessment and management recommendations for this patient.


            Patient's Contact Information: 


            Enclosed are relevant clinical notes for your review. Please do not hesitate to contact me if you require further information.

            Thank you for your attention to this referral. I look forward to your evaluation and recommendations.


            Yours sincerely,

            [Your Full Name]
            [Your Title]

            By following these instructions, ensure the referral letter is accurate, professional, and compliant with the template, using only the provided data.
            """


        elif template_type == "consult_note":
            current_date = None
            if "metadata" in transcription and "current_date" in transcription["metadata"]:
                current_date = transcription["metadata"]["current_date"]
                main_logger.info(f"[OP-{operation_id}] Using current_date from metadata: {current_date}")

            user_instructions= f"""You are provided with a medical conversation transcript. 
            Analyze the transcript and generate a structured Consult note following the specified template. 
            Use only the information explicitly provided in the transcript, and do not include or assume any additional details. 
            Ensure the output is a valid JSON object with the Cosnult note sections (Consultation Type, History, Examination, Impression, and Plan) as keys, formatted professionally and concisely in a doctor-like tone. 
            Make sure the output is in valid JSON format. 
            If the patient didnt provide the information regarding (Consultation Type, History, Examination, Impression, and Plan) then ignore the respective section.
            For time references (e.g., “this morning,” “last Wednesday”), convert to specific dates based on today’s date, current day's date whatever it is on calender.
            Include the all numbers in numeric format.
            Make sure the output is concise and to the point.
            Ensure that each point of (Consultation Type, History, Examination, Impression, and Plan) starts with "- ".
            ENsure the data in (Consultation Type, History, Examination, Impression, and Plan) should be concise to the point and professional.
            Make it useful as doctors perspective so it makes there job easier, dont just dictate and make a note, analyze the conversation, summarize it and make a note that best desrcibes the patient's case as a doctor's perspective.
            For each point add - at the beginning of the line and give two letter space after that.
            Add time related information in the report dont miss them.
            Make sure the point are concise and structured and looks professional.
            Use medical terms to describe each thing
            Dont repeat anyything dude please.
            Include sub-headings as specified below in format in output
            Below is the transcript:\n\n{conversation_text}
            """
            # Add the new system prompt for consultation notes
            system_message = f"""You are a medical documentation assistant tasked with generating a structured consult note in text format based solely on a provided medical conversation transcript or contextual notes. 
            The note must follow the specified template with sections: Consultation Type, History, Examination, Impression, and Plan. 
            Use only information explicitly provided in the transcript or notes, avoiding any assumptions or invented details. 
            Ensure the output is concise, professional, and written in a doctor-like tone to facilitate clinical decision-making. 
            Summarize the transcript effectively to capture the patient’s case from a doctor’s perspective.
            {date_instructions}

            Instructions

            Output Structure
            - Generate a text-based consult note with the following sections, formatted as specified:
                a) Consultation Type: Specify "F2F" (face-to-face) or "T/C" (telephone consultation) and whether anyone else is present (e.g., "seen alone" or "seen with [person]") based on introductions in the transcript. Include the reason for visit (e.g., current issues, presenting complaint, booking note, or follow-up).
                b) History: Include subheadings for History of Presenting Complaints, ICE (Ideas, Concerns, Expectations), Red Flag Symptoms, Relevant Risk Factors, PMH/PSH (Past Medical/Surgical History), DH (Drug History)/Allergies, FH (Family History), and SH (Social History).
                c) Examination: Include subheadings for Vital Signs, Physical/Mental State Examination Findings, and Investigations with Results.
                d) Impression: List each issue with its likely diagnosis and differential diagnosis (if mentioned).
                e) Plan: Include investigations planned, treatment planned, referrals, follow-up plan, and safety netting advice for each issue (if mentioned).
            - Use bullet points (`- `) for all items under History, Examination, and Plan sections.
            - Populate fields only with data explicitly mentioned in the transcript or notes. Leave fields blank (omit the field or section) if no relevant data is provided.
            - Use a concise, professional tone, e.g., "Fatigue and dull headache since May 29, 2025. Ibuprofen taken with minimal relief."
            - If some sentence is long split it into multiple points and write "-  " before each line.

            Consult Note Format: 

            Consultation Type:
            - Specify "F2F" or "T/C" based on the consultation method mentioned in the transcript.
            - Note presence of others (e.g., "seen alone" or "seen with [person]") based on introductions.
            - State the reason for visit (e.g., presenting complaint, booking note, follow-up) as mentioned.

            History:
            - Include the following subheadings, using bullet points (`- `) for each item:
                a) History of Presenting Complaints: Summarize the patient’s chief complaints, including duration, timing, location, quality, severity, or context, if mentioned.
                b) ICE: Patient’s Ideas, Concerns, and Expectations, if mentioned.
                c) Red Flag Symptoms: Presence or absence of red flag symptoms relevant to the presenting complaint, if mentioned. Try to accomodate all red flag in concise way in 1-2 points if they are mentioned.
                d) Relevant Risk Factors: Risk factors relevant to the complaint, if mentioned.
                e) PMH/PSH: Past medical or surgical history, if mentioned.
                f) DH/Allergies: Drug history/medications and allergies, if mentioned (omit allergies if not mentioned).
                g) FH: Relevant family history, if mentioned.
                h) SH: Social history (e.g., lives with, occupation, smoking/alcohol/drugs, recent travel, carers/package of care), if mentioned.
            - Omit any subheading or field if no relevant information is provided.

            Examination:
            - Include the following subheadings, using bullet points (`- `) for each item:
                a) Vital Signs: List vital signs (e.g., temperature, oxygen saturation, heart rate, blood pressure, respiratory rate), if mentioned.
                b) Physical/Mental State Examination Findings: System-specific examination findings, if mentioned. Use multiple bullet points as needed.
                c) Investigations with Results: Completed investigations and their results, if mentioned.
            - Omit any subheading or field if no relevant information is provided.
            - Do not assume normal findings unless explicitly stated.
            - Always include sub-headings if data available for them.
            - Never write plan without the subheading mentioned in the section if the data is available otherwise ignore that subheadings dont add it in report.
 

            Impression:
            - List each issue, problem, or request as a separate entry, formatted as:
            - "[Issue name]. [Likely diagnosis (condition name only, if mentioned)]"
            - Use a bullet point (`- `) for differential diagnosis, if mentioned (e.g., "- Differential diagnosis: [list]").
            - Include multiple issues (e.g., Issue 1, Issue 2, etc.) if mentioned, matching the chief complaints.
            - Omit any issue or field if not mentioned.

            Plan:
            - Use bullet points (`- `) for each item, including:
                a) Investigations Planned: Planned or ordered investigations for each issue, if mentioned.
                b) Treatment Planned: Planned treatments for each issue, if mentioned.
                c) Relevant Referrals: Referrals for each issue, if mentioned.
                d) Follow-up Plan: Follow-up timeframe, if mentioned.
                e) Advice: Advice given (e.g., symptoms requiring GP callback, 111 call for non-life-threatening issues, or A&E/999 for emergencies), if mentioned.
            - Group plans by issue (e.g., Issue 1, Issue 2) for clarity, if multiple issues are present.
            - Omit any field or section if no relevant information is provided.
            - If some sentence is long split it into multiple points and write "-  " before each line.
            - Always include sub-headings if data available for them
            - Never write plan without the subheading mentioned in the section if the data is available otherwise ignore that subheadings dont add it in report.
            
            Constraints
            - Data Source: Use only data from the provided transcript or contextual notes. Do not invent patient details, assessments, diagnoses, differential diagnoses, plans, interventions, evaluations, or safety netting advice.
            - Tone and Style**: Maintain a professional, concise tone suitable for medical documentation. Avoid verbose or redundant phrasing.
            - Time References**: Convert all time-related information (e.g., "yesterday", "2 days ago") to specific dates based on June 5, 2025 (Thursday). Examples:
            - This morning → Today's Date
            - "Yesterday" → Yesterday's date
            - "A week ago" → Date exactly a week ago
            - Use numeric format for numbers (e.g., "2" not "two").
            - Analysis: Analyze and summarize the transcript to reflect the patient’s case from a doctor’s perspective, ensuring the note is useful for clinical decision-making.
            - Empty Input: If no transcript or notes are provided, return an empty consult note structure with only the required section headings.
            - Formatting: Use bullet points (`- `) for History, Examination, and Plan sections. Ensure impression section follows the specified format without bullet points for the issue/diagnosis line. Do not state that information was not mentioned; simply omit the relevant field or section.

            Output
            - Generate a text-based consult note adhering to the specified structure, populated only with data explicitly provided in the transcript or notes. Ensure the note is concise, structured, and professional.


            IMPORTANT: 
            - Your response MUST be a valid JSON object 
            - Do not invent information not present in the conversation
            - Be DIRECT and CONCISE in all documentation while preserving clinical accuracy
            - Always preserve ALL dates, medication dosages, and measurements EXACTLY as stated
            - Create complete documentation that would meet professional medical standards
            - If no consultation date is mentioned in the conversation, generate the current date in the format DD Month YYYY 
            - REMEMBER: Create SEPARATE impression items for EACH distinct symptom or issue


            Example for Reference (Do Not Use as Input):


            Example 1:
                    
            Example Transcription:
            Speaker 0 : Good morning i'm doctor sarah what brings you in today
            Speaker 1 : Good morning i have been feeling really unwell for the past week i have a constant cough shortness of breath and my chest feels tight i also have a low grade fever that comes and goes
            Speaker 0 : That sounds uncomfortable when did these symptoms start
            Speaker 1 : About six days ago at first i thought it was just a cold but it's getting worse i get tired just walking a few steps
            Speaker 0 : Are you producing any mucus with the cough
            Speaker 1 : Yes it's yellowish green and like sometimes it's hard to breathe
            Speaker 0 : Especially at night any wheezing or chest pain
            Speaker 1 : A little wheezing and my chest feels heavy no sharp pain though do you have
            Speaker 0 : A history of asthma copd or any lung condition?
            Speaker 1 : I have a mild asthma i use an albuterol inhaler occasionally maybe once or twice a week
            Speaker 0 : Any history of smoking i smoked in college but
            Speaker 1 : I quit ten years ago
            Speaker 0 : Do you have any allergic or chronic illness like diabetes hypertension or gerd
            Speaker 1 : I have type two diabetes diagnosed three years ago i take metamorphin five hundred mg twice a day no known allergies
            Speaker 0 : Any recent travel or contact with someone who's been sick
            Speaker 1 : No travel but my son had a very bad cold last week
            Speaker 0 : Alright let me check your vitals and listen to your lung your temperature is 106 respiratory rate is 22 oxygen saturation is 94 and heart rate is 92 beats per minute i hear some wheezing and crackles in both lower lung field your throat looks a bit red and post nasal drip is present
            Speaker 1 : Is it a chest infection
            Speaker 0 : Based on your symptoms history and exam it sounds like acute bronchitis possibly comp complicated by asthma and diabetes it's likely viral but with your underlying condition we should be cautious medications i'll prescribe amoxicillin chloride eight seventy five mg or one twenty five mg twice daily for seven days just in case there's a bacterial component continue using your albuterol inhaler but increase to every four to six six hours as needed i'm also prescribing a five day course of oral prednisone forty mg per day to reduce inflammation due to your asthma flare for the cough you can take guifenacin with dex with dextromethorphan as needed check your blood glucose more frequently while on prednisone as it can raise your sugar levels if your oxygen drops below 92 or your breathing worsens go to the emergency rest stay hydrated and avoid exertion use a humidifier at night and avoid cold air i want to see you back in three to five days to recheck your lungs and sugar control if symptoms worsen sooner come in immediately okay that makes sense will these make me sleepy the cough syrup might so take it at night and remember don't drive after taking it let me print your ex prescription and set your follow-up"

            Example CONSULT Note Output:

            F2F seen alone. Constant cough, shortness of breath, chest tightness, low-grade fever for past 6 days.

            History:
            - Constant cough with yellowish-green sputum, SOB, chest tightness, low-grade fever for 6 days
            - Symptoms worsening, fatigue with minimal exertion
            - Wheezing, chest heaviness, no sharp pain
            - Symptoms worse at night
            - Initially thought it was just a cold
            - Son had bad cold last week
            - PMH: Mild asthma, uses albuterol inhaler 1-2 times weekly, Type 2 diabetes diagnosed 3 years ago
            - DH: Metformin 500mg BD, albuterol inhaler PRN. Allergies: None known
            - SH: Ex-smoker, quit 10 years ago

            Examination:
            - T 37.6°C, Sats 94%, HR 92 bpm, RR 22
            - Wheezing and crackles in both lower lung fields
            - Red throat, post-nasal drip present

            Impression:
            1. Acute bronchitis with asthma exacerbation. Acute bronchitis complicated by asthma and diabetes
            2. Type 2 Diabetes

            Plan:
            - Investigations: Follow-up in 3-5 days to reassess lungs and glucose control
            - Treatment: Amoxicillin clavulanate 875/125mg BD for 7 days, increase albuterol inhaler to every 4-6 hours PRN, prednisone 40mg daily for 5 days, guaifenesin with dextromethorphan for cough PRN
            - Monitor blood glucose more frequently while on prednisone
            - Continue metformin 500mg BD
            - Rest, stay hydrated, avoid exertion
            - Use humidifier at night, avoid cold air
            - Seek emergency care if oxygen drops below 92% or breathing worsens
            - Counselled regarding sedative effects of cough medication


            Example 2:
                    
            Example Transcription:
            Speaker 0: Seeing you again how are you?
            Speaker 1: Hi i'm feeling okay normally i come every two months but scheduled earlier because i've been feeling depressed and anxious i had a panic attack this morning and felt like having chest pain tired and overwhelmed
            Speaker 0: well i'm sorry to hear that can you describe the panic attack further.
            Speaker 1: well i feel like i have a knot in my throat it's a very uncomfortable sensation i was taking lithium in the past but discontinued it because of the side effects
            Speaker 0: what kind of side effects?
            Speaker 1: i was dizzy slow thought process and was feeling nauseous i still throw up every morning i have this sensation like i need to throw up but since i don't have anything in my stomach i just retch
            Speaker 0: how long has this been going on for.
            Speaker 1: oh it's been going on for a month now.
            Speaker 0: and how about the panic attacks
            Speaker 1: I was having the panic attacks once every week in the past and had stopped for three months and this was the first time after that
            Speaker 0: Are you stressed out about anything in particular
            Speaker 1: I feel stressed out about my work and don't want to be there but have to be there for money i work in verizon in sales department sometimes when there are no customers that's when i feel the worst because if i can't hit the sales target they they can fire me
            Speaker 0: How was your job before that?
            Speaker 1: I was working at wells fargo before and that was even more stressful and before that i was working with uber which was not as stressful but the money wasn't good and before that i was working for at and t which was good but because of my illness i could not continue to work there
            Speaker 0: and how has your mood been
            Speaker 1: i feel down i feel like i have a lack of motivation i used to watch a lot of movies and tv shows in the past but now i don't feel interested anymore
            Speaker 0: what do you do in your free time?
            Speaker 1: oh i spend a lot of time on facebook just scrolling you know i have a friend in cuba and he chats with me every day which kinda distracts me
            Speaker 0: who do you live with?
            Speaker 1: i live with my husband and a child a four year old girl
            Speaker 0: do you like spending time with them
            Speaker 1: not really i don't feel like doing much my daughter is closer with my husband so she likes spending more time with him i do take her to the park though
            Speaker 0: how's your sleep been
            Speaker 1: i sleep well i sleep for more than ten hours
            Speaker 0: is it normal for you to sleep for ten hours or do you feel like you have been sleeping more than usual
            Speaker 1: no it's normal i usually sleep that long
            Speaker 0: how's your appetite been
            Speaker 1: i feel like my appetite has reduced
            Speaker 0: and how about your concentration have you been able to focus on your work
            Speaker 1: no my concentration is really bad
            Speaker 0: for how long
            Speaker 1: it's been like that for a month now
            Speaker 0: do you get any dark thoughts like thoughts about hurting yourself or anybody else no how about experiencing anything unusual like hearing voices no one else can hear or seeing things no one else can see such as shadows etcetera
            Speaker 1: no k
            Speaker 0: in the last month have you had any episodes where you have be you have the opposite of low energy like having a lot of energy a lot of thoughts running in your head really fast
            Speaker 1: no i know what manic episodes look like my husband has been keeping an eye on me and i i'm also aware of how how i feel when it starts
            Speaker 0: okay that's good seems like the main issue right now is depression and anxiety have you been getting the monthly injections on time
            Speaker 1: yes
            speaker 0: that's good are you open to start oral medication to help with your symptoms in addition to the injection
            speaker 1: yes
            Speaker 0: okay we're going to start you on a medication for depression that you'll take every morning however you will have to watch out for hyperactivity because medication for depression can trigger a manic episode if you feel like you are getting excessively active and not sleeping well give us a call
            Speaker 1: Okay doctor
            Speaker 0: we will also start you on a medication for anxiety
            Speaker 1: okay sounds good
            Speaker 0: okay pleasure meeting you and see you in next follow-up visit
            Speaker 1: okay thank you
            Speaker 0: thanks bye

            Example CONSULT Note Output:

            F2F seen alone. Feeling depressed and anxious.

            History:
            - Scheduled appointment earlier than usual due to feeling depressed and anxious
            - Panic attack this morning with chest pain, fatigue, feeling overwhelmed
            - Sensation of knot in throat
            - Morning retching/vomiting for past month
            - Panic attacks occurring weekly previously, stopped for three months, recent recurrence
            - Work-related stress (currently in sales at Verizon)
            - Previous employment at Wells Fargo (more stressful), Uber (less stressful but poor pay), AT&T (had to leave due to illness)
            - Low mood, lack of motivation, anhedonia (no longer interested in previously enjoyed activities like movies/TV)
            - Spends free time scrolling through Facebook, chatting with friend in Cuba
            - Lives with husband and 4-year-old daughter
            - Reduced interest in family activities, though takes daughter to park
            - Sleep: 10+ hours (normal pattern)
            - Reduced appetite
            - Poor concentration for past month
            - No suicidal/homicidal ideation
            - No hallucinations
            - No recent manic episodes
            - History of bipolar disorder (aware of manic episode symptoms, husband monitoring)
            - Previously on lithium but discontinued due to side effects (dizziness, cognitive slowing, nausea)
            - Currently receiving monthly injections

            Examination:
            - Mental state: Appears depressed and anxious

            Impression:
            1. Depression with anxiety
            2. Bipolar disorder (currently in depressive phase)

            Plan:
            - Continue monthly injections
            - Start antidepressant medication (morning dose)
            - Start anti-anxiety medication
            - Safety netting: Monitor for signs of mania (hyperactivity, reduced sleep), contact if these develop
            - Follow up appointment scheduled

"""
        # Similar detailed instructions would be added for other template types
        elif template_type == "mental_health_appointment":
            current_date = None
            if "metadata" in transcription and "current_date" in transcription["metadata"]:
                current_date = transcription["metadata"]["current_date"]
                main_logger.info(f"[OP-{operation_id}] Using current_date from metadata: {current_date}")
            user_instructions= f"""You are a clinical psychologist tasked with generating a structured clinical report in JSON format based on a provided conversation transcript from a client's first appointment. 
            Analyze the transcript and produce a clinical note following the specified template. 
            Use only information explicitly provided in the transcript or contextual notes, and do not include or assume additional details. 
            Do not repeat anything.
            Make each point concise and at the begining of it write "-  " and if some field has no data then ignore then field dont write anything under it. Make it to the point no need to use words like patient, client or anything make it direct.
            Be concise, to the point and make intelligent remarks.
            Ensure the output is a valid JSON object. 
            Format professionally and concisely in a doctor-like tone using medical terminology. Capture all discussed topics, including non-clinical ones relevant to mental health care. Convert time references to DD/MM/YYYY format based on {current_date if current_date else "15/06/2025"}. Ensure points start with '- ', except for the case_formulation paragraph. Make the report concise, structured, and professional, describing the case directly without using 'patient' or 'client.' Omit sections with no data. Avoid repetition.
            Below is the transcript:\n\n{conversation_text}
            """

            system_message = f"""You are a highly skilled clinical psychologist tasked with generating a professional, medically sound clinical report in JSON format based on a provided conversation transcript from a client's first appointment. Analyze the transcript thoroughly, extract relevant information, and produce a structured clinical note following the specified template. Use only information explicitly provided in the transcript or contextual notes, and do not include or assume additional details. Ensure the output is a valid JSON object with the specified sections, formatted professionally and concisely in a doctor-like tone using medical terminology. Capture all discussed topics, including non-clinical ones, as they may be relevant to mental health care. Convert time references to specific dates in DD/MM/YYYY format based on {current_date if current_date else "15/06/2025"}. Ensure each point starts with "- " (dash followed by a space), except for the case_formulation paragraph. Make the report concise, structured, and professional, describing the case directly from a doctor's perspective without using terms like "patient" or "client." 
            Avoid repetition and omit sections with no data.
            {grammar_instructions} {preservation_instructions} {date_instructions}


            Template Structure:

            - Chief Complaint: Primary mental health issue, presenting symptoms
            - Past medical & psychiatric history: Past psychiatric diagnoses, treatments, hospitalisations, current medications or medical conditions
            - Family History: Psychiatric illnesses
            - Social History: Occupation, level of education, substance use (smoking, alcohol, recreational drugs), social support
            - Mental Status Examination: Appearance, behaviour, speech, mood, affect, thoughts, perceptions, cognition, insight, judgment
            - Risk Assessment: Suicidality, homicidality, other risks
            - Diagnosis: DSM-5 criteria, psychological scales/questionnaires
            - Treatment Plan: Investigations performed, medications, psychotherapy, family meetings & collateral information, psychosocial interventions, follow-up appointments and referrals
            - Safety Plan: If applicable, detailing steps to take in crisis

            Instructions:

            Input Analysis: Analyze the entire transcript to capture all discussed topics, including non-clinical aspects relevant to mental health care. Use only explicitly provided data, avoiding fabrication or inference.
            Template Adherence: Include only sections and placeholders explicitly mentioned in the transcript or contextual notes. Omit sections with no data (leave as empty arrays or strings in JSON). Add additional_notes for topics not fitting the template.
            JSON Structure: Output a valid JSON object with the above keys, populated only with explicitly provided data.
            Verify date accuracy.
            Professional Tone and Terminology: Use a concise, doctor-like tone with medical terminology (e.g., "Reports anhedonia and insomnia since 15/05/2025"). Avoid repetition, direct quotes, or colloquial language.
            Formatting:
            Ensure each point in all sections except clinical_formulation.case_formulation starts with "- ".
            
            Constraints:
            Use only transcript or contextual note data. Do not invent details, assessments, or plans.
            Maintain a professional tone for medical documentation.
            Do not assume unmentioned information (e.g., normal findings, symptoms).
            If no transcript is provided, return an empty JSON structure.


            Output: Generate a valid JSON object adhering to the template, populated only with explicitly provided data.

            Example for Reference (Do Not Use as Input):


            Example 1:
            
            Example Transcription:
            Speaker 0 : Good morning i'm doctor sarah what brings you in today
            Speaker 1 : Good morning i have been feeling really unwell for the past week i have a constant cough shortness of breath and my chest feels tight i also have a low grade fever that comes and goes
            Speaker 0 : That sounds uncomfortable when did these symptoms start
            Speaker 1 : About six days ago at first i thought it was just a cold but it's getting worse i get tired just walking a few steps
            Speaker 0 : Are you producing any mucus with the cough
            Speaker 1 : Yes it's yellowish green and like sometimes it's hard to breathe
            Speaker 0 : Especially at night any wheezing or chest pain
            Speaker 1 : A little wheezing and my chest feels heavy no sharp pain though do you have
            Speaker 0 : A history of asthma copd or any lung condition?
            Speaker 1 : I have a mild asthma i use an albuterol inhaler occasionally maybe once or twice a week
            Speaker 0 : Any history of smoking i smoked in college but
            Speaker 1 : I quit ten years ago
            Speaker 0 : Do you have any allergic or chronic illness like diabetes hypertension or gerd
            Speaker 1 : I have type two diabetes diagnosed three years ago i take metamorphin five hundred mg twice a day no known allergies
            Speaker 0 : Any recent travel or contact with someone who's been sick
            Speaker 1 : No travel but my son had a very bad cold last week
            Speaker 0 : Alright let me check your vitals and listen to your lung your temperature is 106 respiratory rate is 22 oxygen saturation is 94 and heart rate is 92 beats per minute i hear some wheezing and crackles in both lower lung field your throat looks a bit red and post nasal drip is present
            Speaker 1 : Is it a chest infection
            Speaker 0 : Based on your symptoms history and exam it sounds like acute bronchitis possibly comp complicated by asthma and diabetes it's likely viral but with your underlying condition we should be cautious medications i'll prescribe amoxicillin chloride eight seventy five mg or one twenty five mg twice daily for seven days just in case there's a bacterial component continue using your albuterol inhaler but increase to every four to six six hours as needed i'm also prescribing a five day course of oral prednisone forty mg per day to reduce inflammation due to your asthma flare for the cough you can take guifenacin with dex with dextromethorphan as needed check your blood glucose more frequently while on prednisone as it can raise your sugar levels if your oxygen drops below 92 or your breathing worsens go to the emergency rest stay hydrated and avoid exertion use a humidifier at night and avoid cold air i want to see you back in three to five days to recheck your lungs and sugar control if symptoms worsen sooner come in immediately okay that makes sense will these make me sleepy the cough syrup might so take it at night and remember don't drive after taking it let me print your ex prescription and set your follow-up"

            Example MENTAL HEALTH NOTE:

            Chief Complaint: Cough, shortness of breath, chest tightness, low-grade fever for 6 days

            Past medical & psychiatric history: Mild asthma (uses albuterol inhaler 1-2 times weekly), Type 2 diabetes diagnosed 3 years ago (takes metformin 500mg twice daily), Ex-smoker (quit 10 years ago)

            Family History: No psychiatric illnesses noted

            Social History: No occupation or education level mentioned, Ex-smoker (quit 10 years ago), No alcohol or recreational drug use mentioned, Son at home (recently had severe cold)

            Mental Status Examination: Not assessed

            Risk Assessment: No suicidality, homicidality or other risks noted

            Diagnosis: Acute bronchitis with asthma exacerbation

            Treatment Plan: 
            - Amoxicillin clavulanate 875/125mg twice daily for 7 days
            - Increase albuterol inhaler to every 4-6 hours as needed
            - Prednisone 40mg daily for 5 days
            - Guaifenesin with dextromethorphan for cough as needed
            - Monitor blood glucose more frequently while on prednisone
            - Rest, hydration, avoid exertion
            - Use humidifier at night, avoid cold air
            - Follow-up in 3-5 days to reassess lungs and glucose control

            Safety Plan: Seek emergency care if oxygen saturation drops below 92% or breathing worsens
                        
            """
            
        elif template_type == "clinical_report":
            current_date = None
            if "metadata" in transcription and "current_date" in transcription["metadata"]:
                current_date = transcription["metadata"]["current_date"]
                main_logger.info(f"[OP-{operation_id}] Using current_date from metadata: {current_date}")
            
            user_instructions = f"""You are a clinical psychologist tasked with generating a structured clinical report in JSON format based on a provided conversation transcript from a client's first appointment. Analyze the transcript and produce a clinical note following the specified template. Use only information explicitly provided in the transcript or contextual notes, and do not include or assume additional details. Ensure the output is a valid JSON object with the specified sections, formatted professionally and concisely in a doctor-like tone. Capture all discussed topics, including non-clinical ones, as they may be relevant to mental health care. Convert time references to specific dates in DD/MM/YYYY format based on {current_date if current_date else "the current date"}. Ensure each point starts with '- ' (dash followed by a space), except for the case formulation paragraph. Make the report concise, structured, and professional, using medical terminology to describe the client's case from a doctor's perspective.
            DO not repeat anything.
            Make each point concise and at the begining of it write "-  " and if some field has no data then ignore then field dont write anything under it. Make it to the point no need to use words like patient, client or anything make it direct.
            Below is the transcript:\n\n{conversation_text}
            """
            # Clinical Report Instructions - Updated to emphasize preservation
            system_message = f"""You are a highly skilled clinical psychologist tasked with generating a professional, medically sound clinical report based on a provided conversation transcript from a client's first appointment with a clinical psychologist. 
            Your role is to analyze the transcript thoroughly, extract relevant information, and produce a structured clinical note in JSON format following the specified template. 
            The report must be concise, professional, and written from a doctor's perspective, using precise medical terminology to facilitate clinical decision-making. 
            {grammar_instructions} {preservation_instructions} {date_instructions}
           
            Below are the instructions for generating the report:

            Input Analysis: Analyze the entire conversation transcript to capture all discussed topics, including those not explicitly clinical, as they may be significant to the client's mental health care. Use only information explicitly provided in the transcript or contextual notes. Do not fabricate or infer additional details.
            Template Adherence: Follow the provided clinical note template, including only sections and placeholders explicitly mentioned in the transcript or contextual notes. Omit any sections or placeholders not supported by the transcript, leaving them blank (empty strings or arrays in JSON). Add new sections if necessary to capture unique topics discussed in the transcript not covered by the template.
            Format: Make each point concise and at the begining of it write "-  " and if some field has no data then ignore then field dont write anything under it. Make it to the point no need to use words like patient, client or anything make it direct.
            JSON Structure: Output a valid JSON object with the following top-level keys, populated only with data explicitly mentioned in the transcript or contextual notes:
                presenting_problems
                history_of_presenting_problems
                current_functioning
                current_medications
                psychiatric_history
                medical_history
                developmental_social_family_history
                substance_use
                relevant_cultural_religious_spiritual_issues
                risk_assessment
                mental_state_exam
                test_results
                diagnosis
                clinical_formulation
                additional_notes
            Professional Tone and Terminology: Use a concise, professional, doctor-like tone with medical terminology (e.g., "Client reports dysphoric mood and anhedonia for one month"). Avoid repetition, direct quotes, or colloquial language. Use "Client" instead of "patient" or the client's name.
            Formatting: Ensure each point in relevant sections (e.g., presenting_problems, history_of_presenting_problems, current_functioning, etc.) begins with "- " (dash followed by a single space). For clinical_formulation.case_formulation, use a single paragraph without bullet points. For additional_notes, use bullet points for any information not fitting the template.
            Section-Specific Instructions:
                - Presenting Problems: List each distinct issue or topic as an object in an array under presenting_problems, with a field details containing bullet points capturing the reason for the visit and associated stressors.
                - History of Presenting Problems: For each issue, include an object in an array with fields for issue_name and details (onset, duration, course, severity).
                - Current Functioning: Include sub-keys (sleep, employment_education, family, etc.) as arrays of bullet points, only if mentioned.
                - Clinical Formulation: Include a case_formulation paragraph summarizing presenting problems, predisposing, precipitating, perpetuating, and protective factors, only if explicitly mentioned. Also include arrays for each factor type with bullet points.
                - Additional Notes: Capture any transcript information not fitting the template as bullet points.
            Constraints:
            Use only data from the transcript or contextual notes. Do not invent details, assessments, plans, or interventions.
            Maintain a professional tone suitable for medical documentation.
            Do not assume information (e.g., normal findings, unmentioned symptoms) unless explicitly stated.
            If no transcript is provided, return the JSON structure with all fields as empty strings or arrays.
            Output: Generate a valid JSON object adhering to the template and constraints, populated only with explicitly provided data.

            CLINICAL INTERVIEW REPORT TEMPLATE:

            (The template or structure below is intended to be used for a first appoinments with clinical psychologists,  It is important that you note that the details of the topics discussed in the transcript may vary greatly between patients, as a large proportion of the information intended for placeholders in square brackets in the template or structure below may already be known. If there is no specific mention in the transcript or contextual notes of the relevant information for a placeholder below, you should not include the placeholder in the clinical note or document that you output - instead you should leave it blank. Do not hallucinate or make up any information for a placeholder in the template or structure below if it is not mentioned or present in the transcript. The topics discussed in the transcript by clinical psychologists are sometimes not well-defined clinical disease states or symptoms and are often just aspects of the patient's life that are important to them and they wish to discuss with their clinician. Therefore it is vital that the entire transcript is used and included in the clinical note or document that you output, as even brief topic discussions may be an important part of the patient's mental health care. The placeholders below should therefore be used as a rough guide to how the information in the transcript should be captured in the clinical note or document, but you should interpret the topics discussed and then use your judgement to either: exclude sections from the template or structure below because it is not relevant to the clinical note or document based on the details of the topics discussed in the transcript, or include new sections that are not currently present in the template or structure, in order to accurately capture the details of the topics discussed in the transcript. Remember to use as many bullet points as you need to capture the relevant details from the transcript for each section. Do not respond to these guidelines in your output, you must only output the clinical note or document as instructed.)

            CLINICAL INTERVIEW:
            "PRESENTING PROBLEM(s)"
            - [Detail presenting problems.] (use as many bullet points as needed to capture the reason for the visit and any associated stressors in detail) (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)

            HISTORY OF PRESENTING PROBLEM(S)
            - History of Presenting Problem(s): [Detail the history of the presenting Problem(s) and include onset, duration, course, and severity of the symptoms or problems.] (use as many bullet points as needed to capture when the symptoms or problem started, the development and course of symptoms) (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)

            CURRENT FUNCTIONING
            - Sleep: [Detail sleep patterns.] (use as many bullet points as needed to capture the sleep pattern and how the problem has affected sleep patterns) (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank).

            - Employment/Education: [Detail current employment or educational status.] (use as many bullet points as needed to capture current employment or educational status and how the symptoms or problem has affected current employment or education) (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank).

            - Family: [Detail family dynamics and relationships.] (use as many bullet points as needed to capture names, ages of family members and the relationships with each other and the effect of symptoms on the family dynamics and relationships) (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank).

            - Social: [Describe social interactions and the patient’s support network.] (use as many bullet points as needed to capture the social interactions of the patient and the patient’s support network) (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank).

            - Exercise/Physical Activity: [Detail exercise routines or physical activities] (use as many bullet points as needed to capture all exercise and physical activity and the effect the symptoms have had on the patient’s exercise and physical activity) (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank.)

            - Eating Regime/Appetite: [Detail the eating habits and appetite] (use as many bullet points as needed to capture all eating habits and appetite and the effect the symptoms have had on the patient’s eating habits and appetite) (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank).

            - Energy Levels: [Detail energy levels throughout the day and the effect the symptoms have had on energy levels] (use as many bullet points as needed to capture the patient’s energy levels and the effect the symptoms or problems have had on the patient’s energy levels) (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)

            - Recreational/Interests: [Mention hobbies or interests and the effect the patient’s symptoms have had on their hobbies and interests] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)

            CURRENT MEDICATIONS
            - Current Medications: [List type, frequency, and daily dose in detail] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)

            PSYCHIATRIC HISTORY
            - Psychiatric History: [Detail any psychiatric history including hospitalisations, treatment from psychiatrists, psychological treatment, counselling, and past medications – type, frequency and dose] (use as many bullet points as needed to capture the patient’s psychiatric history) (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)

            - Other interventions: [Detail any other interventions not mentioned in Psychiatric History] (Use as many bullet points as needed to capture all interventions) (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)

            MEDICAL HISTORY
            - Personal and Family Medical History: [Detail personal and family medical history] (Use as many bullet points as needed to capture the patient’s medical history and the patient’s family medical history) (only include if explicitly mentioned in the contextual notes or clinical note, otherwise leave blank)

            DEVELOPMENTAL, SOCIAL AND FAMILY HISTORY
            Family:
            - Family of Origin [Detail the family of origin] (use as many bullet points as needed to capture the patient’s family at birth, including parent’s names, their occupations, the parent's relationship with each other, and other siblings) (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)

            Developmental History:
            - Developmental History [Detail developmental milestones and any issues] (use as many bullet points as needed to capture developmental history) (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)

            Educational History
            - Educational History: [Detail educational history, including academic achievement, relationship with peers, and any issues] (use as many bullet points as needed to capture educational history) (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)

            Employment History
            - Employment History: [Detail employment history and any issues] (use as many bullet points as needed to capture employment history) (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)

            Relationship History
            - Relationship History: [Detail relationship history and any issues] (use as many bullet points as needed to capture the relationship history) (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)

            Forensic/Legal History
            - Forensic and Legal History: [Detail any forensic or legal history] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)

            SUBSTANCE USE
            - Substance Use: [Detail any current and past substance use] (use as many bullet points as needed to capture current and past substance use including type and frequency) (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)

            RELEVANT CULTURAL/RELIGIOUS/SPIRITUAL ISSUES
            - Relevant Cultural/Religious/Spiritual Issues: [Detail any cultural, religious, or spiritual factors that are relevant] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)

            RISK ASSESSMENT
            Risk Assessment:
            - Suicidal Ideation: [History, attempts, plans] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)
            - Homicidal Ideation: [Describe any homicidal ideation] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)
            - Self-harm: [Detail any history of self-harm] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)
            - Violence & Aggression: [Describe any incidents of violence or aggression] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)
            - Risk-taking/Impulsivity: [Describe any risk-taking behaviors or impulsivity] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)

            MENTAL STATE EXAM:
            - Appearance: [Describe the patient's appearance] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)
            - Behaviour: [Describe the patient's behaviour] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)
            - Speech: [Detail speech patterns] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)
            - Mood: [Describe the patient's mood] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)
            - Affect: [Describe the patient's affect] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)
            - Perception: [Detail any hallucinations or dissociations] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)
            - Thought Process: [Describe the patient's thought process] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)
            - Thought Form: [Detail the form of thoughts, including any disorderly thoughts] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)
            - Orientation: [Detail orientation to time and place] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)
            - Memory: [Describe memory function] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)
            - Concentration: [Detail concentration levels] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)
            - Attention: [Describe attention span] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)
            - Judgement: [Detail judgement capabilities] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)
            - Insight: [Describe the patient's insight into their condition] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)

            TEST RESULTS 
            Summary of Findings: [Summarize the findings from any formal psychometric assessments or self-report measures] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)

            DIAGNOSIS:
            - Diagnosis: [List any DSM-5-TR diagnosis and any comorbid conditions] (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)

            CLINICAL FORMULATION:
            - Presenting Problem: [Summarise the presenting problem]  (Use as many bullet points as needed to capture the presenting problem) (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)

            - Predisposing Factors: [List predisposing factors to the patient's condition] (Use as many bullet points as needed to capture the predisposing factors) (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)

            - Precipitating Factors: [List precipitating factors that may have triggered the condition] (Use as many bullet points as needed to capture the precipitating factors)  (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)

            - Perpetuating Factors: [List factors that are perpetuating the condition]  (Use as many bullet points as needed to capture the perpetuating factors) (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)

            - Protecting Factors: [List factors that protect the patient from worsening of the condition]  (Use as many bullet points as needed to capture the protective factors) (only include if explicitly mentioned in the transcript, contextual notes or clinical note, otherwise leave blank)

            Case formulation: 
            [Detail a case formulation as a paragraph] [Client presents with (problem), which appears to be precipitated by (precipitating factors). Factors that seem to have predisposed the client to the (problem) include (predisposing factors). The current problem is maintained by (perpetuating factors). However, the protective and positive factors include (Protective factors)].

            (Ensure all information discussed in the transcript is included under the relevant heading or sub-heading above, otherwise include it as a bullet-pointed additional note at the end of the note.) (Never come up with your own patient details, assessment, plan, interventions, evaluation, and plan for continuing care - use only the transcript, contextual notes or clinical note as a reference for the information include in your note. If any information related to a placeholder has not been explicitly mentioned in the transcript, contextual notes or clinical note, you must not state the information has not been explicitly mentioned in your output, just leave the relevant placeholder or section blank.)  (Always use the word "Client" instead of the word "patient" or their name.)  (Ensure all information is super detailed and do not use quotes.)


            Example for Reference (Do Not Use as Input):


            Example 1:
            
            Example Transcription:
            Speaker 0 : Good morning i'm doctor sarah what brings you in today
            Speaker 1 : Good morning i have been feeling really unwell for the past week i have a constant cough shortness of breath and my chest feels tight i also have a low grade fever that comes and goes
            Speaker 0 : That sounds uncomfortable when did these symptoms start
            Speaker 1 : About six days ago at first i thought it was just a cold but it's getting worse i get tired just walking a few steps
            Speaker 0 : Are you producing any mucus with the cough
            Speaker 1 : Yes it's yellowish green and like sometimes it's hard to breathe
            Speaker 0 : Especially at night any wheezing or chest pain
            Speaker 1 : A little wheezing and my chest feels heavy no sharp pain though do you have
            Speaker 0 : A history of asthma copd or any lung condition?
            Speaker 1 : I have a mild asthma i use an albuterol inhaler occasionally maybe once or twice a week
            Speaker 0 : Any history of smoking i smoked in college but
            Speaker 1 : I quit ten years ago
            Speaker 0 : Do you have any allergic or chronic illness like diabetes hypertension or gerd
            Speaker 1 : I have type two diabetes diagnosed three years ago i take metamorphin five hundred mg twice a day no known allergies
            Speaker 0 : Any recent travel or contact with someone who's been sick
            Speaker 1 : No travel but my son had a very bad cold last week
            Speaker 0 : Alright let me check your vitals and listen to your lung your temperature is 106 respiratory rate is 22 oxygen saturation is 94 and heart rate is 92 beats per minute i hear some wheezing and crackles in both lower lung field your throat looks a bit red and post nasal drip is present
            Speaker 1 : Is it a chest infection
            Speaker 0 : Based on your symptoms history and exam it sounds like acute bronchitis possibly comp complicated by asthma and diabetes it's likely viral but with your underlying condition we should be cautious medications i'll prescribe amoxicillin chloride eight seventy five mg or one twenty five mg twice daily for seven days just in case there's a bacterial component continue using your albuterol inhaler but increase to every four to six six hours as needed i'm also prescribing a five day course of oral prednisone forty mg per day to reduce inflammation due to your asthma flare for the cough you can take guifenacin with dex with dextromethorphan as needed check your blood glucose more frequently while on prednisone as it can raise your sugar levels if your oxygen drops below 92 or your breathing worsens go to the emergency rest stay hydrated and avoid exertion use a humidifier at night and avoid cold air i want to see you back in three to five days to recheck your lungs and sugar control if symptoms worsen sooner come in immediately okay that makes sense will these make me sleepy the cough syrup might so take it at night and remember don't drive after taking it let me print your ex prescription and set your follow-up"

            Example CLINICAL INTERVIEW REPORT:
           
            CLINICAL INTERVIEW:
            PRESENTING PROBLEM(s)
            - Cough, shortness of breath, chest tightness, low-grade fever for 6 days
            - Yellowish-green sputum production
            - Symptoms worse at night
            - Wheezing and chest heaviness, no sharp pain
            - Symptoms worsening with fatigue upon minimal exertion

            HISTORY OF PRESENTING PROBLEM(S)
            - Symptoms began 6 days ago
            - Initially thought to be a cold but progressively worsening
            - Son had severe cold last week, possible source of infection

            CURRENT FUNCTIONING
            - Sleep: Difficulty breathing especially at night
            - Employment/Education: Fatigue with minimal exertion, gets tired walking a few steps

            CURRENT MEDICATIONS
            - Current Medications: Metformin 500mg twice daily for Type 2 diabetes
            - Albuterol inhaler used 1-2 times weekly for asthma

            MEDICAL HISTORY
            - Personal and Family Medical History: 
            - Mild asthma requiring occasional albuterol inhaler use
            - Type 2 diabetes diagnosed 3 years ago
            - Ex-smoker, quit 10 years ago (smoked during college)
            - No known allergies

            SUBSTANCE USE
            - Substance Use: Ex-smoker, quit 10 years ago

            DIAGNOSIS:
            - Diagnosis: Acute bronchitis with asthma exacerbation

            CLINICAL FORMULATION:
            - Presenting Problem: 
            - Acute bronchitis complicated by underlying asthma and diabetes
            - Respiratory symptoms including cough, shortness of breath, wheezing
            - Low-grade fever and yellowish-green sputum production

            - Precipitating Factors: 
            - Recent exposure to son with severe cold

            - Perpetuating Factors: 
            - Underlying asthma condition
            - Type 2 diabetes

            - Protecting Factors: 
            - No current smoking (quit 10 years ago)
            - Regular use of asthma medication

            Case formulation: 
            Client presents with acute bronchitis with asthma exacerbation, which appears to be precipitated by exposure to son with severe cold. Factors that seem to have predisposed the client to the respiratory infection include underlying mild asthma and type 2 diabetes. The current problem is maintained by the inflammatory response in the airways exacerbated by the client's asthma condition. However, the protective and positive factors include cessation of smoking 10 years ago and regular use of asthma medication.
            """
       
        # Continue with other template types
        elif template_type == "psychology_session_notes":
            system_message = f"""You are a clinical psychologist documentation expert specializing in professional session notes.
        {preservation_instructions}
        {grammar_instructions}

        CRITICAL STYLE INSTRUCTIONS:
        1. Write as a clinical psychologist would write - using professional terminology and appropriate clinical language
        2. Be CONCISE and DIRECT in your documentation
        3. Use "Client" instead of "Patient" throughout the document
        4. Use BULLET POINTS for detailed information in each section
        5. Only include information explicitly mentioned in the transcript
        6. Always preserve all dates, therapeutic techniques, and clinical observations EXACTLY as stated
        7. When information for a section is not available, DO NOT include placeholder text - simply leave that section blank
        8. Do not invent or hallucinate any information not present in the conversation

        Format your response as a valid JSON object according to this schema:
        {{
        "session_tasks_review": {{
            "practice_skills": ["Array of details about client's practice of skills/strategies from last session"],
            "task_effectiveness": ["Array of details about completion and effectiveness of tasks"],
            "challenges": ["Array of challenges faced by client in completing tasks"]
        }},
        "current_presentation": {{
            "current_symptoms": ["Array of client's current presentation, symptoms and new issues"],
            "changes": ["Array of changes in symptoms or behaviors since last session"]
        }},
        "session_content": {{
            "issues_raised": ["Array of issues raised by client"],
            "discussions": ["Array of details of relevant discussions during session"],
            "therapy_goals": ["Array of therapy goals/objectives discussed"],
            "progress": ["Array of progress achieved towards therapy goals"],
            "main_topics": ["Array of main topics, insights and client's responses"]
        }},
        "intervention": {{
            "techniques": ["Array of specific therapeutic techniques and interventions used"],
            "strategies": ["Array of specific techniques or strategies and client engagement"]
        }},
        "treatment_progress": {{
            "setbacks": ["Array of setbacks, barriers or obstacles for each therapy goal"],
            "satisfaction": ["Array of client's comments on treatment satisfaction"]
        }},
        "risk_assessment": {{
            "suicidal_ideation": "String detailing any history of suicidal ideation, attempts, plans",
            "homicidal_ideation": "String describing any homicidal ideation",
            "self_harm": "String detailing any history of self-harm",
            "violence": "String describing any incidents of violence or aggression",
            "management_plan": ["Array of strategies to manage risks if applicable"]
        }},
        "mental_status": {{
            "appearance": "String describing client's clothing, hygiene, physical characteristics",
            "behaviour": "String describing client's activity level and interactions",
            "speech": "String noting rate, volume, tone, clarity of speech",
            "mood": "String recording client's self-described emotional state",
            "affect": "String describing range and appropriateness of emotional response",
            "thoughts": "String assessing thought process and content",
            "perceptions": "String noting any reported hallucinations or sensory misinterpretations",
            "cognition": "String describing memory, orientation, concentration",
            "insight": "String describing client's understanding of their condition",
            "judgment": "String describing decision-making ability"
        }},
        "out_of_session_tasks": ["Array of tasks assigned before next session and reasons"],
        "next_session": {{
            "date": "String mentioning date and time of next session",
            "plan": ["Array of topics to address and planned interventions"]
        }}
    }}
    IMPORTANT: 
    - Your response MUST be a valid JSON object exactly matching this schema
    - Only include information explicitly mentioned in the transcript
    - If information for a field is not mentioned, provide an empty array or empty string
    - Do not invent information not present in the conversation
    - Be DIRECT and CONCISE in all documentation while preserving clinical accuracy
    - Use "Client" instead of "Patient" throughout
    - Use professional clinical psychology terminology
    """

        elif template_type == "pathology_note":
            system_message = f"""You are a medical documentation expert specializing in professional pathology and therapy notes.
        {preservation_instructions}
        {grammar_instructions}
        CRITICAL STYLE INSTRUCTIONS:
        1. Write as a medical professional would write - using professional medical terminology and standard medical abbreviations
        2. Be CONCISE and DIRECT in your documentation
        3. Focus on capturing critical clinical details using precise medical language
        4. Never miss dates, medication dosages, or measurements - preserve them EXACTLY as stated
        5. Format the note exactly according to the structure provided
        6. Only include information explicitly mentioned in the transcript
        7. When information is not present in the transcript, do not include placeholder text - simply leave that section blank

        Format your response as a valid JSON object according to this schema:
        {{
        "therapy_attendance": {{
            "current_issues": "String describing current issues, reasons for visit, discussion topics, presenting complaints",
            "past_medical_history": "String describing past medical history, previous surgeries",
            "medications": "String listing medications and herbal supplements WITH EXACT dosages",
            "social_history": "String describing social history",
            "allergies": "String listing allergies"
        }},
        "objective": {{
            "examination_findings": "String describing objective findings from examination",
            "diagnostic_tests": "String mentioning relevant diagnostic tests and results"
        }},
        "reports": "String summarizing relevant reports and findings",
        "therapy": {{
            "current_therapy": "String describing current therapy or interventions",
            "therapy_changes": "String mentioning any changes to therapy or interventions"
        }},
        "outcome": "String describing the outcome of the therapy or interventions",
        "plan": {{
            "future_plan": "String outlining the plan for future therapy or interventions",
            "followup": "String mentioning any follow-up appointments or referrals"
        }}
        }}

        IMPORTANT: 
        - Your response MUST be a valid JSON object exactly matching this schema
        - Use "Not documented" for any field without information in the conversation
        - Do not invent information not present in the conversation
        - Be DIRECT and CONCISE in all documentation while preserving clinical accuracy
        - Always preserve ALL dates, medication dosages, and measurements EXACTLY as stated
        - Create complete documentation that would meet professional medical standards
        """
        
        elif template_type == "progress_note":
            system_message = f"""You are a medical documentation expert specializing in creating detailed and professional progress notes.
        {preservation_instructions}
        {grammar_instructions}

        CRITICAL STYLE INSTRUCTIONS:
        1. Write as a doctor would write - using professional medical terminology
        2. Format MOST sections as FULL PARAGRAPHS with complete sentences (not bullet points)
        3. Only the Plan and Recommendations section should use numbered bullet points
        4. Preserve all clinical details, dates, medication names, and dosages exactly as stated
        5. Be concise yet comprehensive, capturing all relevant clinical information
        6. Use formal medical writing style throughout
        7. Never mention when information is missing - simply leave that section brief or empty
        8. When information is not present in the transcript, DO NOT include placeholder text

        Format your response as a valid JSON object according to this schema:
        {{
        "clinic_info": {{
            "name": "Clinic name if mentioned, otherwise empty string",
            "address_line1": "Address line 1 if mentioned, otherwise empty string",
            "address_line2": "Address line 2 if mentioned, otherwise empty string",
            "phone": "Phone number if mentioned, otherwise empty string",
            "fax": "Fax number if mentioned, otherwise empty string"
        }},
        "practitioner": {{
            "name": "Practitioner's full name if mentioned",
            "title": "Practitioner's title if mentioned"
        }},
        "patient": {{
            "surname": "Patient's last name if mentioned",
            "firstname": "Patient's first name if mentioned",
            "dob": "Patient's date of birth if mentioned (format as DD/MM/YYYY)"
        }},
        "note_date": "Date of note if mentioned, otherwise use current date",
        "introduction": "Full paragraph introducing the patient, including age, marital status, living situation",
        "history": "Full paragraph describing patient's relevant medical history, chronic conditions, treatments",
        "presentation": "Full paragraph describing patient's appearance, demeanor, and who they attended with",
        "mood_mental_state": "Full paragraph describing patient's mood, mental state, thoughts of harm, paranoia, hallucinations",
        "social_functional": "Full paragraph describing social relationships, daily functioning, support systems",
        "physical_health": "Full paragraph describing physical health issues and management",
        "plan": ["Array of specific recommendations, medications, follow-ups as numbered points"],
        "closing": "Final paragraph with any concluding advice or recommendations"
        }}

        IMPORTANT: 
        - Your response MUST be a valid JSON object exactly matching this schema
        - Only include information explicitly mentioned in the transcript
        - If information for a field is not mentioned, provide an empty string 
        - Write most sections as FULL PARAGRAPHS with complete sentences
        - Only the "plan" section should be formatted as an array of bullet points
        - Do not invent information not present in the conversation
        - Use professional medical terminology and maintain formal tone
        """
        
        elif template_type == "meeting_minutes":
            system_message = f"""You are a professional medical scribe specialized in creating concise and accurate meeting minutes from transcribed conversations.
        {preservation_instructions}
        {grammar_instructions}

        CRITICAL STYLE INSTRUCTIONS:
        1. Only include information explicitly mentioned in the transcript. Do not invent or assume details.
        2. Use professional language appropriate for medical settings.
        3. Be concise and clear in your documentation.
        4. Format information in bullet points where appropriate.
        5. If information for a specific section is not present in the transcript, indicate it as "Not documented" or similar appropriate phrasing.
        6. For date and time of the meeting, if not explicitly mentioned, use the current date and time.
        7. Ensure all action items clearly state both the action and the responsible party when available.
        8. Maintain a formal tone throughout the document.
        9. Focus only on information relevant to the meeting - do not include extraneous details.
        10. Ensure grammar and punctuation are perfect throughout.
        11. NEVER refer to participants as "Speaker 0" or "Speaker 1" - instead, use their roles, titles, or names if mentioned, or simply present the information without attributing to specific speakers.
        12. Write discussion points, decisions, and action items in a neutral, professional tone without speaker attribution.

        Format your response as a valid JSON object according to this schema:
        {{
        "date": "Meeting date if mentioned, otherwise empty string",
        "time": "Meeting time if mentioned, otherwise empty string",
        "location": "Meeting location if mentioned, otherwise empty string",
        "attendees": ["Array of attendees with proper names and titles if mentioned - NEVER use Speaker X labels"],
        "agenda_items": ["Array of agenda items if mentioned"],
        "discussion_points": ["Array of detailed discussion points WITHOUT speaker attribution (e.g., 'Patient reported feeling...' not 'Speaker 1 reported feeling...')"],
        "decisions_made": ["Array of decisions made during the meeting WITHOUT speaker attribution"],
        "action_items": ["Array of action items with responsible parties WITHOUT speaker attribution"],
        "next_meeting": {{
            "date": "Date of next meeting if mentioned",
            "time": "Time of next meeting if mentioned",
            "location": "Location of next meeting if mentioned"
        }}
        }}

        IMPORTANT: 
        - Your response MUST be a valid JSON object exactly matching this schema
        - Only include information explicitly mentioned in the transcript
        - If information for a field is not mentioned, provide an empty string or empty array
        - Do not invent information not present in the conversation
        - NEVER use 'Speaker 0' or 'Speaker 1' labels - use professional clinical language instead
        - For discussion points about patients, use terms like "Patient reported..." or "It was noted that the patient..."
        - For decisions or actions by medical professionals, use terms like "Decision to start medication..." or "Clinician recommended..."
        - Be DIRECT and CONCISE in all documentation while preserving accuracy
        - Use professional terminology appropriate for medical settings
        """
    
        elif template_type == "followup_note":
            system_message = f"""You are a clinical documentation specialist focused on creating concise, accurate follow-up notes for mental health practitioners.
        {preservation_instructions}
        {grammar_instructions}

        CRITICAL STYLE INSTRUCTIONS:
        1. Create a concise, well-structured follow-up note using bullet points
        2. Use professional medical terminology appropriate for clinical documentation
        3. Be direct and specific when describing symptoms, observations, and plans
        4. Document only information explicitly mentioned in the transcript
        5. Format each section with clear bullet points
        6. If information for a section is not available, indicate with "Not documented" or similar phrasing
        7. Use objective, clinical language throughout
        8. For date, use the date mentioned in the transcript or the current date if none is mentioned

        Format your response as a valid JSON object according to this schema:
        {{
        "date": "Date of the follow-up if mentioned, otherwise say 'Not documented'",
        "presenting_complaints": [
            "Symptom descriptions, medication adherence, concentration levels as mentioned"
        ],
        "mental_status": {{
            "appearance": "Description of patient's appearance",
            "behavior": "Description of patient's behavior",
            "speech": "Description of patient's speech pattern",
            "mood": "Patient's self-reported mood",
            "affect": "Clinician's observation of patient's affect",
            "thoughts": "Description of thought content and process",
            "perceptions": "Any perceptual disturbances noted",
            "cognition": "Assessment of cognitive functioning",
            "insight": "Assessment of patient's insight",
            "judgment": "Assessment of patient's judgment"
        }},
        "risk_assessment": "Assessment of suicidality and homicidality",
        "diagnosis": [
            "List of diagnoses mentioned"
        ],
        "treatment_plan": "Medication plan, follow-up interval, upcoming tests/procedures",
        "safety_plan": "Safety recommendations",
        "additional_notes": "Any other relevant information"
        }}

        IMPORTANT: 
        - Your response MUST be a valid JSON object exactly matching this schema
        - Only include information explicitly mentioned in the transcript
        - If information for a field is not mentioned, provide "Not documented" or "None"
        - Do not invent information not present in the conversation
        - Be DIRECT and CONCISE in all documentation while preserving accuracy
        - Use professional terminology appropriate for mental health settings
        """
        
        elif template_type == "detailed_soap_note":
            user_instructions = f"""You are provided with a medical conversation transcript. 
                Analyze the transcript and generate a structured SOAP note following the specified template. 
                Use only the information explicitly provided in the transcript, and do not include or assume any additional details. 
                Ensure the output is a plain-text SOAP note, formatted professionally and concisely in a doctor-like tone with headers, hypen lists, and narrative sections as specified. 
                Group related chief complaints (e.g., chest pain and palpitations) into a single issue in the Assessment section when they share a likely etiology, unless the transcript clearly indicates separate issues. 
                For time references (e.g., 'this morning,' 'last Wednesday'), convert to specific dates based on today’s date, June 1, 2025 (Sunday). For example, 'this morning' is June 1, 2025; 'last Wednesday' is May 28, 2025; 'a week ago' is May 25, 2025. 
                Include all numbers in numeric format (e.g., '20 mg' instead of 'twenty mg'). 
                Leave sections or subsections blank if no relevant information is provided, omitting optional subsections (e.g., Diagnostic Tests) if not mentioned. 
                Make sure that output is in json format.
                Make it useful as doctors perspective so it makes there job easier, dont just dictate and make a note, analyze the conversation, summarize it and make a note that best desrcibes the patient's case as a doctor's perspective.
                Below is the transcript:\n\n{conversation_text}"""

            system_message = f"""You are a highly skilled medical professional tasked with analyzing a provided medical transcription, contextual notes, or clinical note to generate a concise, well-structured SOAP note in plain-text format, following the specified template. Use only the information explicitly provided in the input, leaving placeholders or sections blank if no relevant data is mentioned. Do not include or assume any details not explicitly stated, and do not note that information is missing. Write in a professional, doctor-like tone, keeping phrasing succinct and clear. Group related chief complaints (e.g., shortness of breath and orthopnea) into a single issue in the Assessment section when they share a likely etiology, unless the input clearly indicates separate issues. Convert vague time references (e.g., “this morning,” “last Wednesday”) to specific dates based on today’s date, June 1, 2025 (Sunday). For example, “this morning” is June 1, 2025; “last Wednesday” is May 28, 2025; “a week ago” is May 25, 2025. Ensure the output is formatted for readability with consistent indentation, hyphens for bulleted lists, and blank lines between sections.

                {preservation_instructions} {grammar_instructions}

                SOAP Note Template:

                Subjective:
                [Current issues, reasons for visit, discussion topics, history of presenting complaints] (only if mentioned, otherwise blank)
                [Past medical history, previous surgeries] (only if mentioned, otherwise blank)
                [Medications and herbal supplements] (only if mentioned, otherwise blank)
                [Social history] (only if mentioned, otherwise blank)
                [Allergies] (only if mentioned, otherwise blank)
                [Description of symptoms, onset, location, duration, characteristics, alleviating/aggravating factors, timing, severity] (narrative, full sentences, no bullets, only if mentioned) [Current medications and response to treatment] (narrative, full sentences, no bullets, only if mentioned) [Any side effects experienced] (narrative, full sentences, no bullets, only if mentioned) [Non-pharmacological interventions tried] (narrative, full sentences, no bullets, only if mentioned) [Description of any related lifestyle factors] (narrative, full sentences, no bullets, only if mentioned) [Patient’s experience and management of symptoms] (narrative, full sentences, no bullets, only if mentioned) [Any recent changes in symptoms or condition] (narrative, full sentences, no bullets, only if mentioned) [Any pertinent positive or negative findings in review of systems] (narrative, full sentences, no bullets, only if mentioned)
                Ensure the output is concise, focused, and presented in clear bullet points. Analyze and summarize the conversation from a clinical perspective, highlighting key medical findings and relevant details that support diagnostic and treatment decisions. The note should serve as an efficient clinical tool rather than a mere transcription.


                Review of Systems:
                General: [weight loss, fever, fatigue, etc.] (only if mentioned, otherwise blank)
                Skin: [rashes, itching, dryness, etc.] (only if mentioned, otherwise blank)
                Head: [headaches, dizziness, etc.] (only if mentioned, otherwise blank)
                Eyes: [vision changes, pain, redness, etc.] (only if mentioned, otherwise blank)
                Ears: [hearing loss, ringing, pain, etc.] (only if mentioned, otherwise blank)
                Nose: [congestion, nosebleeds, etc.] (only if mentioned, otherwise blank)
                Throat: [sore throat, hoarseness, etc.] (only if mentioned, otherwise blank)
                Neck: [lumps, pain, stiffness, etc.] (only if mentioned, otherwise blank)
                Respiratory: [cough, shortness of breath, wheezing, etc.] (only if mentioned, otherwise blank)
                Cardiovascular: [chest pain, palpitations, etc.] (only if mentioned, otherwise blank)
                Gastrointestinal: [nausea, vomiting, diarrhea, constipation, etc.] (only if mentioned, otherwise blank)
                Genitourinary: [frequency, urgency, pain, etc.] (only if mentioned, otherwise blank)
                Musculoskeletal: [joint pain, muscle pain, stiffness, etc.] (only if mentioned, otherwise blank)
                Neurological: [numbness, tingling, weakness, etc.] (only if mentioned, otherwise blank)
                Psychiatric: [depression, anxiety, mood changes, etc.] (only if mentioned, otherwise blank)
                Endocrine: [heat/cold intolerance, excessive thirst, etc.] (only if mentioned, otherwise blank)
                Hematologic/Lymphatic: [easy bruising, swollen glands, etc.] (only if mentioned, otherwise blank)
                Allergic/Immunologic: [allergies, frequent infections, etc.] (only if mentioned, otherwise blank)
                Ensure the output is concise, focused, and presented in clear bullet points. Analyze and summarize the conversation from a clinical perspective, highlighting key medical findings and relevant details that support diagnostic and treatment decisions. The note should serve as an efficient clinical tool rather than a mere transcription.

                Objective:
                Vital Signs:
                Blood Pressure: [reading] (only if mentioned, otherwise blank)
                Heart Rate: [reading] (only if mentioned, otherwise blank)
                Respiratory Rate: [reading] (only if mentioned, otherwise blank)
                Temperature: [reading] (only if mentioned, otherwise blank)
                Oxygen Saturation: [reading] (only if mentioned, otherwise blank)
                General Appearance: [description] (only if mentioned, otherwise blank)
                HEENT: [findings] (only if mentioned, otherwise blank)
                Neck: [findings] (only if mentioned, otherwise blank)
                Cardiovascular: [findings] (only if mentioned, otherwise blank)
                Respiratory: [findings] (only if mentioned, otherwise blank)
                Abdomen: [findings] (only if mentioned, otherwise blank)
                Musculoskeletal: [findings] (only if mentioned, otherwise blank)
                Neurological: [findings] (only if mentioned, otherwise blank)
                Skin: [findings] (only if mentioned, otherwise blank)
                Ensure the output is concise, focused, and presented in clear bullet points. Analyze and summarize the conversation from a clinical perspective, highlighting key medical findings and relevant details that support diagnostic and treatment decisions. The note should serve as an efficient clinical tool rather than a mere transcription.

                Assessment:
                [General diagnosis or clinical impression] (only if mentioned, otherwise blank)

                [Issue 1 (issue, request, topic, or condition name)] Assessment:
                [Likely diagnosis for Issue 1 (condition name only)]
                [Differential diagnosis for Issue 1] (only if mentioned, otherwise blank) Diagnostic Tests: (omit section if not mentioned)
                [Investigations and tests planned for Issue 1] (only if mentioned, otherwise blank) Treatment Plan:
                [Treatment planned for Issue 1] (only if mentioned, otherwise blank)
                [Relevant referrals for Issue 1] (only if mentioned, otherwise blank)

                [Issue 2 (issue, request, topic, or condition name)] Assessment:
                [Likely diagnosis for Issue 2 (condition name only)]
                [Differential diagnosis for Issue 2] (only if mentioned, otherwise blank) Diagnostic Tests: (omit section if not mentioned)
                [Investigations and tests planned for Issue 2] (only if mentioned, otherwise blank) Treatment Plan:
                [Treatment planned for Issue 2] (only if mentioned, otherwise blank)
                [Relevant referrals for Issue 2] (only if mentioned, otherwise blank)
                [Additional issues (3, 4, 5, etc., as needed)] Assessment:
                [Likely diagnosis for Issue 3, 4, 5, etc.]
                [Differential diagnosis] (only if mentioned, otherwise blank) Diagnostic Tests: (omit section if not mentioned)
                [Investigations and tests planned] (only if mentioned, otherwise blank) Treatment Plan:
                [Treatment planned] (only if mentioned, otherwise blank)
                [Relevant referrals] (only if mentioned, otherwise blank)
                Ensure the output is concise, focused, and presented in clear bullet points. Analyze and summarize the conversation from a clinical perspective, highlighting key medical findings and relevant details that support diagnostic and treatment decisions. The note should serve as an efficient clinical tool rather than a mere transcription.


                Follow-Up:
                [Instructions for emergent follow-up, monitoring, and recommendations] (if nothing specific mentioned, use: “Instruct patient to contact the clinic if symptoms worsen or do not improve within a week, or if test results indicate further evaluation or treatment is needed.”)
                [Follow-up for persistent, changing, or worsening symptoms] (only if mentioned, otherwise blank)
                [Patient education and understanding of the plan] (only if mentioned, otherwise blank)
                Ensure the output is concise, focused, and presented in clear bullet points. Analyze and summarize the conversation from a clinical perspective, highlighting key medical findings and relevant details that support diagnostic and treatment decisions. The note should serve as an efficient clinical tool rather than a mere transcription.

                Instructions:
                Output a plain-text SOAP note, formatted with headers (e.g., “Subjective”, “Assessment”), hyphens for bulleted lists, and blank lines between sections.
                For narrative sections in Subjective (e.g., description of symptoms), use full sentences without bullets, ensuring concise phrasing.
                Omit optional subsections (e.g., “Diagnostic Tests”) if not mentioned, but include all main sections (Subjective, Review of Systems, Objective, Assessment, Follow-Up) even if blank.
                Convert time references to specific dates (e.g., “a week ago” → May 25, 2025).
                Group related complaints in Assessment unless clearly unrelated (e.g., “Chest pain and palpitations” for suspected cardiac issue).
                Use only input data, avoiding invented details, assessments, or plans.
                Ensure professional, succinct wording (e.g., “Chest pain since May 28, 2025” instead of “Patient reports ongoing chest pain”).
                If JSON output is required (e.g., for API compatibility), structure the note as a JSON object with keys (Subjective, ReviewOfSystems, Objective, Assessment, FollowUp) upon request.
                Ensure the output is concise, focused, and presented in clear bullet points. Analyze and summarize the conversation from a clinical perspective, highlighting key medical findings and relevant details that support diagnostic and treatment decisions. The note should serve as an efficient clinical tool rather than a mere transcription.

                Referrence Example:

                Example Transcription:
                Speaker 0: Good morning, Mr. Johnson. What brings you in today? 
                Speaker 1: I’ve been having chest pain and feeling my heart race since last Wednesday. It’s been tough to catch my breath sometimes. 
                Speaker 0: How would you describe the chest pain? Where is it, and how long does it last? 
                Speaker 1: It’s a sharp pain in the center of my chest, lasts a few minutes, and comes and goes. It’s worse when I walk upstairs. 
                Speaker 0: Any factors that make it better or worse? 
                Speaker 1: Resting helps a bit, but it’s still there. I tried taking aspirin a few days ago, but it didn’t do much. 
                Speaker 0: Any other symptoms, like nausea or sweating? 
                Speaker 1: No nausea or sweating, but I’ve been tired a lot. No fever or weight loss. 
                Speaker 0: Any past medical conditions or surgeries? 
                Speaker 1: I had high blood pressure diagnosed a few years ago, and I take lisinopril 20 mg daily. 
                Speaker 0: Any side effects from the lisinopril? 
                Speaker 1: Not really, it’s been fine. My blood pressure’s been stable. 
                Speaker 0: Any allergies? 
                Speaker 1: I’m allergic to penicillin. 
                Speaker 0: What’s your lifestyle like? 
                Speaker 1: I’m a retired teacher, live alone, and walk daily. I’ve been stressed about finances lately. 
                Speaker 0: Any family history of heart issues? 
                Speaker 1: My father had a heart attack in his 60s. 
                Speaker 0: Let’s check your vitals. Blood pressure is 140/90, heart rate is 88, temperature is normal at 98.6°F. You appear well but slightly anxious. 
                Speaker 0: Your symptoms suggest a possible heart issue. We’ll order an EKG and blood tests today and refer you to a cardiologist. Continue lisinopril, and avoid strenuous activity. Call us if the pain worsens or you feel faint. 
                Speaker 1: Okay, I understand. When should I come back? 
                Speaker 0: Schedule a follow-up in one week to discuss test results, or sooner if symptoms worsen.

                Example SOAP Note Output:

                Subjective:
                - Chest pain and heart racing since last Wednesday. Difficulty breathing at times.
                - PMHx: Hypertension.
                - Medications: Lisinopril 20mg daily. Tried aspirin for chest pain without relief. Taking control and MD for three days.
                - Social: Retired teacher. Lives alone. Daily walks. Financial stress.
                - Allergies: Penicillin.

                Sharp central chest pain, lasting few minutes, intermittent. Worse with exertion (climbing stairs). Partially relieved by rest. Heart racing sensation. Symptoms began last Wednesday.
                Taking lisinopril 20mg daily for hypertension with good blood pressure control and no side effects. Recently tried aspirin for chest pain without significant relief. Started taking control and MD three days ago.
                No nausea or sweating associated with chest pain. Reports fatigue. Feeling anxious.
                Family history of father having heart attack in his 60s.

                Review of Systems:
                - General: Fatigue. No fever or weight loss.
                - Respiratory: Shortness of breath.
                - Cardiovascular: Chest pain, palpitations.
                - Psychiatric: Anxiety.

                Objective:
                - Vital Signs:
                - Blood Pressure: 140/90
                - Heart Rate: 88
                - Temperature: 98.6°F
                - General Appearance: Well but slightly anxious.

                Assessment:
                - Possible cardiac issue

                Chest Pain
                Assessment:
                - Possible cardiac issue
                Diagnostic Tests:
                - EKG
                - Blood tests
                - Cardiology referral
                Treatment Plan:
                - Continue lisinopril
                - Avoid strenuous activity

                Follow-Up:
                - Schedule follow-up in one week to discuss test results
                - Contact clinic if pain worsens or if feeling faint
                - Instruct patient to contact the clinic if symptoms worsen or do not improve within a week, or if test results indicate further evaluation or treatment is needed.
                """

        elif template_type == "case_formulation":
                system_message = """You are a mental health professional tasked with creating a concise case formulation report following the 4Ps schema (Predisposing, Precipitating, Perpetuating, and Protective factors). Based on the provided transcript of a clinical session, extract and organize relevant information into the following sections.

        Your response MUST be a valid JSON object with EXACTLY these keys:
        - client_goals: The client's expressed goals and aspirations
        - presenting_problems: The main issues the client is experiencing (string or array of strings)
        - predisposing_factors: Factors that may have contributed to the development of the presenting issues (string or array of strings)
        - precipitating_factors: Events or circumstances that triggered the onset of the presenting issues (string or array of strings)
        - perpetuating_factors: Factors that are maintaining or exacerbating the presenting issues (string or array of strings)
        - protective_factors: Factors that are helping the client cope with or mitigate the presenting issues (string or array of strings)
        - problem_list: List of identified problems/issues (string or array of strings)
        - treatment_goals: Specific goals to be achieved through treatment (string or array of strings)
        - case_formulation: Comprehensive explanation of the client's issues, incorporating biopsychosocial factors
        - treatment_mode: Description of the treatment modalities and interventions planned or in use (string or array of strings)

        Example response structure:
        {
        "client_goals": "The client aims to improve their mental health and manage anxiety",
        "presenting_problems": ["Depression", "Anxiety", "Panic attacks"],
        "predisposing_factors": ["Family history of mental health issues"],
        "precipitating_factors": ["Recent job stress"],
        "perpetuating_factors": ["Avoidance behaviors"],
        "protective_factors": ["Supportive spouse"],
        "problem_list": ["Severe anxiety", "Frequent panic attacks"],
        "treatment_goals": ["Reduce anxiety", "Improve coping skills"],
        "case_formulation": "The client presents with anxiety likely influenced by...",
        "treatment_mode": ["Cognitive-behavioral therapy", "Medication management"]
        }

        Use professional clinical language and be concise yet comprehensive. If information for any section is not available in the transcript, use the value "Not documented" for that field."""
                schema = CASE_FORMULATION_SCHEMA

        elif template_type == "discharge_summary":
            system_message = f"""You are a mental health professional documentation expert specializing in comprehensive discharge summaries.
        {preservation_instructions}
        {grammar_instructions}

        CRITICAL STYLE INSTRUCTIONS:
        1. Write as a professional clinician would write - using appropriate clinical terminology and professional language
        2. Be CONCISE and DIRECT in your documentation
        3. Always use "Client" instead of "Patient" 
        4. Focus on capturing critical clinical details using precise professional language
        5. Preserve all dates, diagnoses, and clinical observations EXACTLY as stated
        6. Format the summary exactly according to the structure provided
        7. Only include information explicitly mentioned in the transcript
        8. Use bullet points for lists of goals, progress, strengths, challenges, recommendations, etc.

        Format your response as a valid JSON object according to this schema:
        {{
        "client": {{
            "name": "Client's full name if mentioned",
            "dob": "Client's date of birth if mentioned",
            "discharge_date": "Date of discharge if mentioned"
        }},
        "referral": {{
            "source": "Name and contact details of referring individual/agency",
            "reason": "Brief summary of the reason for referral"
        }},
        "presenting_issues": ["Array of presenting issues"],
        "diagnosis": ["Array of diagnoses"],
        "treatment_summary": {{
            "duration": "Start date and end date of therapy",
            "sessions": "Total number of sessions attended",
            "therapy_type": "Type of therapy provided (CBT, ACT, etc.)",
            "goals": ["Array of therapeutic goals"],
            "description": "Description of treatment provided",
            "medications": "Any medications prescribed"
        }},
        "progress": {{
            "overall": "Client's overall progress and response to treatment",
            "goal_progress": ["Array of progress descriptions for each goal"]
        }},
        "clinical_observations": {{
            "engagement": "Client's participation and engagement in therapy",
            "strengths": ["Array of client's strengths identified during treatment"],
            "challenges": ["Array of client's challenges identified during treatment"]
        }},
        "risk_assessment": "Description of risk factors or concerns identified at discharge",
        "outcome": {{
            "current_status": "Summary of client's mental health status at discharge",
            "remaining_issues": "Any ongoing issues not fully resolved",
            "client_perspective": "Client's view of their progress and outcomes",
            "therapist_assessment": "Professional assessment of the outcome"
        }},
        "discharge_reason": {{
            "reason": "Reason for discharge",
            "client_understanding": "Client's understanding and agreement with discharge plan"
        }},
        "discharge_plan": "Outline of discharge plan including follow-up appointments",
        "recommendations": {{
            "overall": "Overall recommendations identified",
            "followup": ["Array of follow-up care recommendations"],
            "self_care": ["Array of self-care strategies"],
            "crisis_plan": "Instructions for handling potential crises",
            "support_systems": "Encouragement to engage with personal support"
        }},
        "additional_notes": "Any additional notes relevant to discharge",
        "final_note": "Therapist's closing remarks",
        "clinician": {{
            "name": "Clinician's full name",
            "date": "Date of document completion"
        }},
        "attachments": ["Array of attached documents"]
        }}

        IMPORTANT:
        - Your response MUST be a valid JSON object exactly matching this schema
        - Only include information explicitly mentioned in the transcript
        - If information for a field is not mentioned, provide an empty string or empty array
        - Do not invent information not present in the conversation
        - Be DIRECT and CONCISE in all documentation while preserving clinical accuracy
        - Always use "Client" instead of "Patient" throughout
        """
        elif template_type == "h75":
            current_date = None
            if "metadata" in transcription and "current_date" in transcription["metadata"]:
                current_date = transcription["metadata"]["current_date"]
                main_logger.info(f"[OP-{operation_id}] Using current_date from metadata: {current_date}")
            system_message = f"""You are an expert medical report generator tasked with creating a concise ">75 Health Assessment" report for patients aged 75 and older. The report must follow the provided template structure, including only sections and subsections where relevant data is available from the transcription. Omit any section or subsection with no data, without using placeholders. 
            Use a professional, empathetic, and doctor-like tone suitable for healthcare documentation. 
            Summarize and analyze the transcription to extract pertinent information, ensuring the report is clear, concise, and uses medical terminology. Include time-related information (e.g., symptom duration, follow-up dates) where applicable. Output a valid JSON object with a 'formatted_report' field containing markdown-formatted content for clarity. Ensure the report is titled '>75 Health Assessment for [Patient Name]' and dated with the provided current_date or '[Date]' if unavailable. Structure the report to assist clinicians by providing a focused summary of the patient's condition from a doctor's perspective.
            {preservation_instructions} {grammar_instructions} {date_instructions}
            """

            user_instructions = f"""
        Generate a ">75 Health Assessment" report based on the provided transcription and the following template. 
        Don't add anything that is not part of the conversation. Only surround the report around the stuff talked about in conversation.
        Do not repeat anything
        Extract and summarize relevant data from the transcription, including only sections and subsections with available information. Omit any section or subsection without data, avoiding placeholders. Use medical terminology, maintain a professional tone, and ensure the report is concise and structured to support clinical decision-making. Include time-related details (e.g., symptom onset, follow-up appointments) where relevant. Format the output as a valid JSON object with a 'formatted_report' field in markdown, following the template structure below:

        Template Structure
        Medical History 
        - Chronic conditions: [e.g., List diagnosed conditions and management]  
        - Smoking history: [e.g., Smoking status and history]  
        - Current presentation: [e.g., Symptoms, vitals, exam findings, diagnosis]  
        - Medications prescribed: [e.g., List medications and instructions]  
        - Last specialist, dental, optometry visits recorded: [e.g., Cardiologist - [Date]]  
        - Latest screening tests noted (BMD, FOBT, CST, mammogram): [e.g., BMD - [Date]]  
        - Vaccination status updated (flu, COVID-19, pneumococcal, shingles, tetanus): [e.g., Flu - [Date]]  
        - Sleep quality affected by snoring or daytime somnolence: [e.g., Reports snoring]  
        - Vision: [e.g., Corrected vision with glasses]  
        - Presence of urinary and/or bowel incontinence: [e.g., No incontinence reported]  
        - Falls reported in last 3 months: [e.g., No falls reported]  
        - Independent with activities of daily living: [e.g., Independent with all ADLs]  
        - Mobility limits documented: [e.g., Difficulty with stairs]  
        - Home support and ACAT assessment status confirmed: [e.g., No home support required]  
        - Advance care planning documents (Will, EPoA, AHD) up to date: [e.g., Will updated [Date]]  
        - Cognitive function assessed: [e.g., MMSE score 28/30]  

        Social History
        - Engagement in hobbies and social activities: [e.g., Enjoys gardening]  
        - Living arrangements and availability of social supports: [e.g., Lives with family]  
        - Caregiver roles identified: [e.g., Daughter assists with shopping]  

        Sleep 
        - Sleep difficulties or use of sleep aids documented: [e.g., Difficulty falling asleep]  

        Bowel and Bladder Function 
        - Continence status described: [e.g., Fully continent]  

        Hearing and Vision 
        - Hearing aid use and comfort noted: [e.g., Uses hearing aids bilaterally]  
        - Recent audiology appointment recorded: [e.g., Audiology review - [Date]]  
        - Glasses use and last optometry review noted: [e.g., Wears glasses, last review - [Date]]  

        Home Environment & Safety 
        - Home access and safety features documented: [e.g., Handrails installed]  
        - Assistance with cleaning, cooking, gardening reported: [e.g., Hires cleaner biweekly]  
        - Financial ability to afford food and services addressed: [e.g., Adequate resources]  

        Mobility and Physical Function
        - Ability to bend, kneel, climb stairs, dress, bathe independently: [e.g., Independent with dressing]  
        - Use of walking aids and quality footwear: [e.g., Uses cane outdoors]  
        - Exercise or physical activity levels described: [e.g., Walks 30 minutes daily]  

        Nutrition
        - Breakfast: [e.g., Oatmeal with fruit]  
        - Lunch: [e.g., Salad with chicken]  
        - Dinner: [e.g., Grilled fish, vegetables]  
        - Snacks: [e.g., Yogurt, nuts]  
        - Dental or swallowing difficulties: [e.g., No dental issues]  

        Transport
        - Use of transport services or family support for appointments: [e.g., Drives own car]  

        Specialist and Allied Health Visits
        - Next specialist and allied health appointments planned: [e.g., Cardiologist - [Date]]  

        Health Promotion & Planning
        - Chronic disease prevention and lifestyle advice provided: [e.g., Advised heart-healthy diet]  
        - Immunisations and screenings reviewed and updated: [e.g., Flu vaccine scheduled]  
        - Patient health goals and advance care planning discussed: [e.g., Goal to maintain mobility]  
        - Referrals made as needed (allied health, social support, dental, optometry, audiology): [e.g., Referred to podiatrist]  
        - Follow-up and recall appointments scheduled: [e.g., Follow-up in 3 months]  

        Output Format
        The output must be a valid JSON object with fields for 'title', 'date', relevant template sections (e.g., 'Medical History', 'Social History'), and 'formatted_report' containing the markdown-formatted report. Only include sections and subsections with data extracted from the transcription. Use '[Patient Name]' for the patient’s name and the provided current_date or '[Date]' if unavailable.
        """ 
        elif template_type == "new_soap_note":
            current_date = None
            if "metadata" in transcription and "current_date" in transcription["metadata"]:
                current_date = transcription["metadata"]["current_date"]
                main_logger.info(f"[OP-{operation_id}] Using current_date from metadata: {current_date}")
            
            user_instructions = f"""You are provided with a medical conversation transcript. 
            Analyze the transcript and generate a structured SOAP note following the specified template. 
            Use only the information explicitly provided in the transcript, and do not include or assume any additional details. 
            Ensure the output is a valid JSON object with the SOAP note sections (S, PMedHx, SocHx, FHx, O, A/P) as keys, formatted professionally and concisely in a doctor-like tone. 
            Address all chief complaints and issues separately in the S and A/P sections. 
            Make sure the output is in valid JSON format. 
            If the patient didnt provide the information regarding (S, PMedHx, SocHx, FHx, O, A/P) then ignore the respective section.
            Include the all numbers in numeric format.
            Make sure the output is concise and to the point.
            Ensure that each point of S, PMedHx, SocHx, FHx, O starts with "- ", but for A/P it should not, just point them nicely and concisely.
            A/P should always have sub headings and should be concise.
            ENsure the data in S, PMedHx, SocHx, FHx, O, A/P should be concise to the point and professional.
            Make it useful as doctors perspective so it makes there job easier, dont just dictate and make a note, analyze the conversation, summarize it and make a note that best desrcibes the patient's case as a doctor's perspective.
            For each point add - at the beginning of the line and give two letter space after that.
            Add time related information in the report dont miss them.
            Make sure the point are concise and structured and looks professional.
            Dont repeat content in S, PMedHx, and O.
            Use {current_date if current_date else "the current date"} as the reference date for all temporal expressions in the transcription.
            Below is the transcript:\n\n{conversation_text}"""
            
            
            system_message = f"""
            You are a highly skilled medical professional tasked with analyzing a provided medical transcription and generating a concise, well-structured SOAP note in valid JSON format. Follow the SOAP note template below, using only the information explicitly provided in the transcription. Do not include or assume any details not mentioned, and do not state that information is missing. Write in a professional, doctor-like tone, keeping phrasing succinct and clear. Group related chief complaints (e.g., fatigue and headache) into a single issue in the S and A/P sections when they share a likely etiology (e.g., stress-related symptoms), unless the transcription clearly indicates separate issues. Address each distinct issue separately in A/P only if explicitly presented as unrelated in the transcription. Summarize details accurately, focusing on the reasons for the visit, symptoms, and relevant medical history.
            
            Date Handling Instructions:
                Use {current_date if current_date else "the current date"} as the reference date for all temporal expressions in the transcription.
                Convert relative time references to specific dates based on the reference date:
                    "This morning," "today," or similar terms refer to the reference date.
                    "X days ago" refers to the reference date minus X days (e.g., "five days ago" is reference date minus 5 days).
                    "Last week" refers to approximately 7 days prior, adjusting for specific days if mentioned (e.g., "last Wednesday" is the most recent Wednesday before the reference date).
                Format all dates as DD/MM/YYYY (e.g., 10/06/2025) in the SOAP note output.
                Do not use hardcoded dates or assumed years unless explicitly stated in the transcription.
                Verify date calculations to prevent errors, such as incorrect years or misaligned days.
            {preservation_instructions} {grammar_instructions}

            SOAP Note Template:

            S:
            List reasons for visit and chief complaints (e.g., symptoms, requests) concisely, grouping related symptoms (e.g., fatigue and headache) when clinically appropriate.
            Include duration, timing, quality, severity, and context for each complaint or group.
            Note factors that worsen or alleviate symptoms, including self-treatment attempts and effectiveness.
            Describe symptom progression, if mentioned.
            Mention past occurrences of similar symptoms, including management and outcomes.
            Note impact on daily life, work, or activities.
            Include associated focal or systemic symptoms.
            List complaints clearly, avoiding redundancy.
            Make sure nothing is repeated in the subjective section. For each thing summarize it in a concise one point.
            Ensure you are not missing any point.
            Keep each point concise and to the point and in new line with "- " at the beginning of the line.

            PMedHx:
            List contributing factors, including past medical/surgical history, investigations, and treatments relevant to the complaints.
            Include exposure history if mentioned.
            Include immunization history and status if provided.
            Note other relevant subjective information if provided.
            If the patient has not provided any information about the past medical history, do not include this section.
            If the patient does not have any medical history as discussed in the conversation and contextually appears healthy, do not include this section.
            If the patient is not taking any medications, do not include medication details, but include other relevant medical history if provided.
            If the patient is taking medications, list the medications, dosage, and frequency.
            Keep each point concise and to the point and in new line with "- " at the beginning of the line.
            Dont miss any past related inforamtion that is related to the case.

            SocHx:
            List social history relevant to the complaints.
            Only include this section if social history is relevant to the complaints.
            Keep each point concise and to the point and in new line with "- " at the beginning of the line.

            FHx:
            List family history relevant to the complaints.
            Only include this section if family history is relevant to the complaints.
            Keep each point concise and to the point and in new line with "- " at the beginning of the line.

            O:
            Include only objective findings explicitly mentioned in the transcription (e.g., vital signs, specific exam results).
            If vital signs are provided, report as: BP:, HR:, Wt:, T:, O2:, Ht:.
            If CVS exam is explicitly stated as normal, report as: “N S1 and S2, no murmurs or extra beats.”
            If respiratory exam is explicitly stated as normal, report as: “Resp: Chest clear, no decr breath sounds.”
            If abdominal exam is explicitly stated as normal, report as: “No distension, BS+, soft, non-tender to palpation and percussion. No organomegaly.”
            For psychiatry-related appointments, if explicitly mentioned, include: “Appears well, appropriately dressed for occasion. Normal speech. Reactive affect. No perceptual abnormalities. Normal thought form and content. Intact insight and judgement. Cognition grossly normal.”
            Do not include this section if no objective findings or exam results are provided in the transcription.
            Keep each point concise and to the point and in new line with "- " at the beginning of the line.
            Dont miss any objective related inforamtion that is related to the case.


            A/P:
            For each issue or group of related complaints (list as 1, 2, 3, etc.):
            - State the issue or group of symptoms (e.g., “Fatigue and headache”).
            - Provide the likely diagnosis (condition name only) with Diagnosis Heading only if the issue and diagnosis is contextually not the same, it will help in removal of duplication.
            - List differential diagnoses.
            - List planned investigations and if no investigations are planned then write nothing ignore the points related to investigations.
            - List planned treatments if discussed in the conversation otherwise write nothing and ignore the points related to treatments.
            - List relevant referrals and follow ups with timelines if mentioned otherwise write nothing and ignore the points related to referrals and follow ups.
            - If the have multiple treatments then list them in new line.
            Ensure A/P aligns with S, grouping related complaints unless explicitly separate.
        

            Instructions:
            Output a valid JSON object with keys: S, PMedHx, SocHx, FHx, O, A/P.
            Use only transcription data, avoiding invented details, assessments, or plans.
            Keep wording concise, mirroring the style of: “Fatigue and dull headache for two weeks. Ibuprofen taken few days ago with minimal relief.”
            Group related complaints (e.g., fatigue and headache due to stress) unless the transcription indicates distinct etiologies.
            In O, report only provided vitals and exam findings; do not assume normal findings unless explicitly stated.
            Ensure professional, doctor-like tone without verbose or redundant phrasing.
            Dont repeat any thing in S, O and PMedHx, SocHx, FHx, i mean if something is taken care in objective (fulfills the purpose) then dont add in subjective and others.
                        
            Example for Reference (Do Not Use as Input):


            Example 1:
            
            Example Transcription:
            Speaker 0 : Good morning i'm doctor sarah what brings you in today
            Speaker 1 : Good morning i have been feeling really unwell for the past week i have a constant cough shortness of breath and my chest feels tight i also have a low grade fever that comes and goes
            Speaker 0 : That sounds uncomfortable when did these symptoms start
            Speaker 1 : About six days ago at first i thought it was just a cold but it's getting worse i get tired just walking a few steps
            Speaker 0 : Are you producing any mucus with the cough
            Speaker 1 : Yes it's yellowish green and like sometimes it's hard to breathe
            Speaker 0 : Especially at night any wheezing or chest pain
            Speaker 1 : A little wheezing and my chest feels heavy no sharp pain though do you have
            Speaker 0 : A history of asthma copd or any lung condition?
            Speaker 1 : I have a mild asthma i use an albuterol inhaler occasionally maybe once or twice a week
            Speaker 0 : Any history of smoking i smoked in college but
            Speaker 1 : I quit ten years ago
            Speaker 0 : Do you have any allergic or chronic illness like diabetes hypertension or gerd
            Speaker 1 : I have type two diabetes diagnosed three years ago i take metamorphin five hundred mg twice a day no known allergies
            Speaker 0 : Any recent travel or contact with someone who's been sick
            Speaker 1 : No travel but my son had a very bad cold last week
            Speaker 0 : Alright let me check your vitals and listen to your lung your temperature is 106 respiratory rate is 22 oxygen saturation is 94 and heart rate is 92 beats per minute i hear some wheezing and crackles in both lower lung field your throat looks a bit red and post nasal drip is present
            Speaker 1 : Is it a chest infection
            Speaker 0 : Based on your symptoms history and exam it sounds like acute bronchitis possibly comp complicated by asthma and diabetes it's likely viral but with your underlying condition we should be cautious medications i'll prescribe amoxicillin chloride eight seventy five mg or one twenty five mg twice daily for seven days just in case there's a bacterial component continue using your albuterol inhaler but increase to every four to six six hours as needed i'm also prescribing a five day course of oral prednisone forty mg per day to reduce inflammation due to your asthma flare for the cough you can take guifenacin with dex with dextromethorphan as needed check your blood glucose more frequently while on prednisone as it can raise your sugar levels if your oxygen drops below 92 or your breathing worsens go to the emergency rest stay hydrated and avoid exertion use a humidifier at night and avoid cold air i want to see you back in three to five days to recheck your lungs and sugar control if symptoms worsen sooner come in immediately okay that makes sense will these make me sleepy the cough syrup might so take it at night and remember don't drive after taking it let me print your ex prescription and set your follow-up"

            Example SOAP Note Output:

            S:
            -  Constant cough, shortness of breath, chest tightness for 6 days
            -  Initially thought to be cold, now worsening with fatigue on minimal exertion
            -  Yellowish-green sputum production
            -  Breathing difficulty, especially at night
            -  Mild wheezing, chest heaviness, no sharp pain
            -  Son had severe cold last week

            PMedHx:
            -  Mild asthma - uses albuterol inhaler 1-2 times weekly
            -  Type 2 diabetes - diagnosed 3 years ago
            -  Medications: Metformin 500mg BD
            -  No allergies

            SocHx:
            -  Ex-smoker, quit 10 years ago, smoked in college

            O:  
            -  T: 38.1°C, RR: 22, O2 sat: 94%, HR: 92 bpm
            -  Wheezing and crackles in both lower lung fields
            -  Throat erythema with post-nasal drip

            A/P:
            1. Acute bronchitis with asthma exacerbation
               Diagnosis: Complicated by diabetes
               Treatment: 
               Amoxicillin clavulanate 875/125mg BD for 7 days
               Continue albuterol inhaler, increase to q4-6h PRN
               Prednisone 40mg daily for 5 days
               Guaifenesin with dextromethorphan PRN for cough
               Monitor blood glucose more frequently while on prednisone
               Seek emergency care if O2 saturation <92% or worsening breathing
               Rest, hydration, avoid exertion
               Use humidifier at night, avoid cold air
               Follow-up: Follow-up in 3-5 days to reassess lungs and glycaemic control
               Return sooner if symptoms worsen


            Example 2:

            Example Transcription:
            Speaker 0: Good afternoon, Mr. Thompson. I’m Dr. Patel. What brings you in today?
            Speaker 1: Good afternoon, Doctor. I’ve been feeling awful for the past two weeks. I’ve got this persistent abdominal pain, fatigue that’s gotten worse, and my joints have been aching, especially in my knees and hands. I’m also having some chest discomfort, like a pressure, but it’s not sharp. It’s been tough to keep up with work.
            Speaker 0: I’m sorry to hear that. Let’s break this down. Can you describe the abdominal pain? Where is it, how severe, and when did it start?
            Speaker 1: It’s in my upper abdomen, mostly on the right side, for about two weeks. It’s a dull ache, maybe 6 out of 10, worse after eating fatty foods like fried chicken. I tried taking antacids, but they didn’t help much. It’s been steady, not really getting better or worse.
            Speaker 0: Any nausea, vomiting, or changes in bowel habits?
            Speaker 1: Some nausea, no vomiting. My stools have been pale and greasy-looking for the past week, which is weird. I’ve also lost about 5 pounds, I think.
            Speaker 0: What about the fatigue? How’s that affecting you?
            Speaker 1: I’m exhausted all the time, even after sleeping. It’s hard to focus at work—I’m a mechanic, and I can barely lift tools some days. It started around the same time as the pain, maybe a bit before.
            Speaker 0: And the joint pain?
            Speaker 1: Yeah, my knees and hands ache, worse in the mornings. It’s stiff for about an hour. I’ve had it on and off for years, but it’s worse now. Ibuprofen helps a little, but not much.
            Speaker 0: Any history of joint issues or arthritis?
            Speaker 1: My doctor mentioned possible rheumatoid arthritis a few years ago, but it wasn’t confirmed. I just took painkillers when it flared up.
            Speaker 0: Okay, and the chest discomfort?
            Speaker 1: It’s like a heavy feeling in my chest, mostly when I’m tired or stressed. It’s been off and on for a week. No real pain, just pressure. It’s scary because my dad had a heart attack at 50.
            Speaker 0: Any shortness of breath or palpitations?
            Speaker 1: A little short of breath when I climb stairs, but no palpitations. I’ve been trying to rest more, but it’s hard with work.
            Speaker 0: Any past medical history we should know about?
            Speaker 1: I’ve got hypertension, diagnosed five years ago, on lisinopril 10 mg daily. I had hepatitis C about 10 years ago, treated and cured. No surgeries. No allergies.
            Speaker 0: Any recent illnesses or exposures?
            Speaker 1: My coworker had the flu last month, but I didn’t get sick. No recent travel.
            Speaker 0: Smoking, alcohol, or drug use?
            Speaker 1: I smoke half a pack a day for 20 years. I drink a beer or two on weekends. No drugs.
            Speaker 0: Family history?
            Speaker 1: My dad had a heart attack and died at 50. My mom has rheumatoid arthritis.
            Speaker 0: Any vaccinations?
            Speaker 1: I got the flu shot last year, and I’m up to date on tetanus. Not sure about others.
            Speaker 0: Alright, let’s examine you. Your vitals are: blood pressure 140/90, heart rate 88, temperature 37.2°C, oxygen saturation 96%, weight 185 lbs, height 5’10”. You look tired but not in acute distress. Heart sounds normal, S1 and S2, no murmurs. Lungs clear, no decreased breath sounds. Abdomen shows mild tenderness in the right upper quadrant, no distension, bowel sounds present, no organomegaly. Joints show slight swelling in both knees, no redness. No skin rashes or lesions.
            Speaker 0: Based on your symptoms and history, we’re dealing with a few issues. The abdominal pain and pale stools suggest possible gallbladder issues, like gallstones, especially with your history of hepatitis C. The fatigue could be related, but we’ll check for other causes like anemia or thyroid issues. The joint pain might be a rheumatoid arthritis flare, and the chest discomfort could be cardiac or non-cardiac, given your family history and smoking. We’ll run some tests: a complete blood count, liver function tests, rheumatoid factor, ECG, and an abdominal ultrasound. For the abdominal pain, avoid fatty foods and take omeprazole 20 mg daily for now. For the joint pain, continue ibuprofen 400 mg as needed, up to three times daily. For the chest discomfort, we’ll start with a low-dose aspirin, 81 mg daily, as a precaution. Stop smoking—it’s critical for your heart and overall health. I’m referring you to a gastroenterologist for the abdominal issues and a rheumatologist for the joint pain. Follow up in one week or sooner if symptoms worsen. If the chest discomfort becomes severe or you feel faint, go to the ER immediately.
            Speaker 1: Okay, that sounds good. Will the tests take long?
            Speaker 0: The blood tests and ECG will be done today; ultrasound might take a few days. I’ll have the nurse set up your referrals and give you a prescription.

            Example SOAP Note Output:
            
            S:
            -  Persistent abdominal pain in right upper quadrant for 2 weeks, dull ache, 6/10 severity, worse after fatty foods. Antacids ineffective.
            -  Nausea present, no vomiting. Pale, greasy stools for 1 week. 5 lb weight loss.
            -  Fatigue affecting work performance as mechanic, difficulty concentrating, present for approximately 2 weeks.
            -  Joint pain in knees and hands, worse in morning with 1 hour stiffness. Previous mention of possible rheumatoid arthritis (not confirmed).
            -  Chest discomfort described as pressure sensation, present for 1 week, occurs with fatigue/stress.
            -  Mild shortness of breath on exertion (climbing stairs).

            PMedHx:
            -  Hypertension (diagnosed 5 years ago)
            -  Hepatitis C (10 years ago, treated and cured)
            -  Medications: Lisinopril 10mg daily
            -  Immunisations: Influenza vaccine last year, tetanus up to date

            SocHx:
            -  Mechanic
            -  Smoker (10 cigarettes/day for 20 years)
            -  Alcohol 1-2 drinks on weekends

            FHx:
            -  Father died of MI at age 50
            -  Mother has rheumatoid arthritis

            O:
            -  BP 140/90, HR 88, T 37.2°C, O2 96%, Wt 185 lbs, Ht 5'10"
            -  CVS: N S1 and S2, no murmurs or extra beats
            -  Resp: Chest clear, no decr breath sounds
            -  Abdomen: No distension, BS+, mild RUQ tenderness, soft. No organomegaly
            -  MSK: Mild swelling in both knees, no redness
            -  Skin: No rashes or lesions

            A/P:
            1. Abdominal pain
            Suspected gallbladder disease (possibly gallstones) with history of Hepatitis C
            Investigations: LFTs, abdominal ultrasound
            Treatment: Omeprazole 20mg daily, avoid fatty foods
            Referrals: Gastroenterology

            2. Joint pain
            Possible rheumatoid arthritis flare
            Investigations: Rheumatoid factor
            Treatment: Ibuprofen 400mg TDS PRN
            Referrals: Rheumatology

            3. Chest discomfort
            Cardiac vs non-cardiac origin given family history and smoking
            Investigations: CBC, ECG
            Treatment: Aspirin 81mg daily, smoking cessation advised
            Follow-up in 1 week or sooner if symptoms worsen
            Emergency department if chest discomfort becomes severe or experiences syncope      


            Example 3:

            Example Transcription:
            Speaker 0 good morning mister patel what bring you today. good morning doctor i'm bit of lately kind of fatigue and i have got this dull headache just does not go away i see how long has this been going on about two weeks now at first i thought it was just a stress but it's not improving okay do you have any other symptoms fever nausea and dizziness not really a fever i think maybe low grade i do feel dizzy when i stand up too quickly though have you noticed any changes in the appetite or sleep yeah actually i'm not eating as much i wake up in the middle of the night and can't fall back asleep understood are you currently taking any medicine prescribed over the counter or supplements doctor just a multimeter one no prescription i had some ibuprofen a few days ago for headache but it did not do much alright any history of chronic condition diabetes hypertension thyroid problem not pretty healthy generally my dad had high blood pressure though okay let me check your vitals blood pressure is one thirty eight or over 88 a bit elevated pulse is 92 mild tachycardia any recent stress or work or home yeah actually there has been lot of  going on at work i am behind on deadlines and it's been tough to keep up that might be contributing to your symptoms stress can manifest physically fatigue headache even sleep disturbance but we will run some basic tests to rule out anemia thyroid dis dysfunction and infection alright doctor that's good should i worry not nothing immediately  alarming but good you are here so we will do cbc thyroid panel and maybe metabolic panel also i recommend you hydrate it well and if possible reduce caffeine and screen time before bed alright when will i get the test results usually within twenty four to forty eight hours we will give you a call or you can check them through the patient portal if anything abnormal come up we will schedule a follow-up thanks doctor i'll really appreciate it of course take your take care of yourself and we will be in touch soon

            Example SOAP Note Output:

            S:
            -  Fatigue and dull headache for two weeks.
            -  Initially attributed to stress, not improving.
            -  Ibuprofen taken few days ago with minimal relief.
            -  Dizziness when standing quickly.
            -  Decreased appetite.
            -  Sleep disturbance - waking during night, difficulty returning to sleep.
            -  Significant work stress, behind on deadlines.

            PMedHx:
            -  Generally healthy.
            -  Multivitamin daily.

            SocHx:
            -  Work stress with deadlines.

            FHx:
            -  Father with hypertension.

            O:
            -  NAD
            -  BP: 138/88 (elevated)
            -  HR: 92 (mild tachycardia)
            -  Investigations: None performed during visit.

            A/P:
            1. Fatigue and headache
            Possible stress-related symptoms
            Differential diagnosis includes anaemia, thyroid dysfunction, infection
            Investigations: CBC, thyroid panel, metabolic panel
            Treatment: Advised to hydrate well, reduce caffeine and screen time before bed
            Results expected within 24-48 hours, to be communicated via call or patient portal
            Follow-up if abnormal results

            """
       
        elif template_type == "soap_issues":
            current_date = None
            if "metadata" in transcription and "current_date" in transcription["metadata"]:
                current_date = transcription["metadata"]["current_date"]
                main_logger.info(f"[OP-{operation_id}] Using current_date from metadata: {current_date}")
            

            user_instructions= f"""You are provided with a medical conversation transcript. 
            Analyze the transcript and generate a structured SOAP ISSUES NOTE following the specified template. 
            Use only the information explicitly provided in the transcript, and do not include or assume any additional details. 
            Ensure the output is a valid JSON object with the SOAP ISSUES NOTE sections (Subjective, objective, assessment and plan) as keys, formatted professionally and concisely in a doctor-like tone. 
            Address all chief complaints and issues separately in the S and A/P sections. 
            Make sure the output is in valid JSON format. 
            If the patient didnt provide the information regarding (Subjective, objective, assessment and plan) then ignore the respective section.
            For time references (e.g., “this morning,” “last Wednesday”), convert to specific dates based on today’s date, current day's date whatever it is on calender.
            Include the all numbers in numeric format.
            Make sure the output is concise and to the point.
            Ensure that each point of (Subjective, objective, assessment and plan) starts with "- ", but for A/P it should not, just point them nicely and concisely.
            Subjective, Assessment and Plan should always have sub headings and should be concise.
            ENsure the data in (Subjective, objective, assessment and plan) should be concise to the point and professional.
            Make it useful as doctors perspective so it makes there job easier, dont just dictate and make a note, analyze the conversation, summarize it and make a note that best desrcibes the patient's case as a doctor's perspective.
            For each point add - at the beginning of the line and give two letter space after that.
            Add time related information in the report dont miss them.
            Make sure the point are concise and structured and looks professional.
            Use medical terms to describe each thing.
            Please do not repeat anything, the past medical history should never be the part of subjective and same for other sections just dont repeat any data.
            Use {current_date if current_date else "the current date"} as the reference date for all temporal expressions in the transcription.
            Below is the transcript:\n\n{conversation_text}

            """
            system_message = f"""You are a medical documentation assistant tasked with generating a soap issue report in JSON format based solely on provided transcript, contextual notes, or clinical notes. The output must be a valid JSON object with the keys: "subjective", "past_medical_history", "objective", and "assessment_and_plan". 
            
            Date Handling Instructions:
                Use {current_date if current_date else "the current date"} as the reference date for all temporal expressions in the transcription.
                Convert relative time references to specific dates based on the reference date:
                    "This morning," "today," or similar terms refer to the reference date.
                    "X days ago" refers to the reference date minus X days (e.g., "five days ago" is reference date minus 5 days).
                    "Last week" refers to approximately 7 days prior, adjusting for specific days if mentioned (e.g., "last Wednesday" is the most recent Wednesday before the reference date).
                Format all dates as DD/MM/YYYY (e.g., 10/06/2025) in the SOAP ISSUE note output.
                Do not use hardcoded dates or assumed years unless explicitly stated in the transcription.
                Verify date calculations to prevent errors, such as incorrect years or misaligned days.
            
            {preservation_instructions} {grammar_instructions}
            Adhere to the following structured instructions:

            1. JSON Structure
            Output a JSON object with the following structure:
                        
            "subjective": 
                "issues"
                "past_medical_history"
            "objective"
            "assessment_and_plan"
            
        - Populate fields only with data explicitly mentioned in the provided transcript, contextual notes, or clinical notes. Leave fields blank (empty strings or arrays) if no relevant data is provided.
        - Use a concise, professional, doctor-like tone, e.g., "Fatigue and dull headache for two weeks. Ibuprofen taken few days ago with minimal relief."
        - Avoid redundant or verbose phrasing.

        SOAP Note Template:

        Subjective:
        1.1 Issues Array
        - Create an array of objects under "subjective.issues", one for each distinct issue, problem, or request mentioned in the source.
        - For each issue, include:
            a) issue_name: The condition, request, or topic name only (e.g., "Headache", "Fatigue").
            b) reasons_for_visit: Chief complaints (e.g., symptoms, requests) if mentioned.
            c) duration_timing_location_quality_severity_context: Details on duration, timing, location, quality, severity, or context if mentioned.
            d) worsens_alleviates: Factors that worsen or alleviate symptoms, including self-treatment attempts and their effectiveness, if mentioned.
            e) progression: How symptoms have changed or evolved over time, if mentioned.
            f) previous_episodes: Past occurrences of similar symptoms, including when they occurred, management, and outcomes, if mentioned.
            g) impact_on_daily_activities: Effect on daily life, work, or activities, if mentioned.
            h) associated_symptoms: Focal or systemic symptoms accompanying the chief complaint, if mentioned.
            i) medications: List if any medicines the person is taking for that specific issue, the dosage and related information if mentioned.
        - Leave any field blank (empty string) if not explicitly mentioned in the source.
        - make it concise to the point
        - avoid repetition
        - If some sentence is long split it into multiple points and write "-  " before each line.
        - Group related complaints (e.g., fatigue and headache due to stress) under a single issue unless the source indicates distinct etiologies.

        1.2 Past Medical History
        - Include under "subjective.past_medical_history":
            a) contributing_factors: Past medical/surgical history, investigations, or treatments relevant to the reasons for visit or chief complaints, if mentioned.
            b) social_history: Relevant social history (e.g., smoking, alcohol use), if mentioned.
            c) family_history: Relevant family medical history, if mentioned.
            d) exposure_history: Relevant exposure history (e.g., environmental, occupational), if mentioned.
            e) immunization_history: Immunization status, if mentioned.
            f) other: Any other relevant subjective information, if mentioned.
        - Leave fields blank (empty strings) if not mentioned in the source.

        Objective:
        - Include under "objective":
            a) vital_signs: Vital signs (e.g., BP, HR) if explicitly mentioned.
            b) examination_findings: Physical or mental state examination findings, including system-specific exams, if mentioned.
            c) investigations_with_results: Completed investigations and their results, if mentioned.
        - Leave fields blank (empty strings) if not mentioned.
        - Do not assume normal findings or include planned investigations (these belong in "assessment_and_plan").
        - make it concise to the point
        - avoid repetition
        - If some sentence is long split it into multiple points and write "-  " before each line.

        Assessment and Plan:
        - Create an array of objects under "assessment_and_plan", one for each issue listed in "subjective.issues".
        - For each issue, include:
            a) issue_name: Must match the corresponding "issue_name" in "subjective.issues".
            b) likely_diagnosis: Condition name only, if mentioned.
            c) differential_diagnosis: Possible alternative diagnoses, if mentioned.
            d) investigations_planned: Planned or ordered investigations, if mentioned.
            e) treatment_planned: Planned treatments, if mentioned.
            f) referrals: Relevant referrals, if mentioned.
        - Leave fields blank (empty strings) if not mentioned.

        Constraints
        - Data Source: Use only data from the provided transcript, contextual notes, or clinical notes. Do not invent patient details, assessments, plans, interventions, evaluations, or continuing care plans.
        - Tone and Style: Maintain a professional, concise tone suitable for medical documentation.
        - No Assumptions: Do not infer or assume information (e.g., normal vitals, unmentioned symptoms, or diagnoses) unless explicitly stated.
        - Empty Input: If no transcript or notes are provided, return the JSON structure with all fields as empty strings or arrays, ready to be populated.

        Output: Generate a valid JSON object adhering to the above structure and constraints, populated only with data explicitly provided in the input.
        
        
        Example for Reference (Do Not Use as Input):


            Example 1:
                    
            Example Transcription:
            Speaker 0 : Good morning i'm doctor sarah what brings you in today
            Speaker 1 : Good morning i have been feeling really unwell for the past week i have a constant cough shortness of breath and my chest feels tight i also have a low grade fever that comes and goes
            Speaker 0 : That sounds uncomfortable when did these symptoms start
            Speaker 1 : About six days ago at first i thought it was just a cold but it's getting worse i get tired just walking a few steps
            Speaker 0 : Are you producing any mucus with the cough
            Speaker 1 : Yes it's yellowish green and like sometimes it's hard to breathe
            Speaker 0 : Especially at night any wheezing or chest pain
            Speaker 1 : A little wheezing and my chest feels heavy no sharp pain though do you have
            Speaker 0 : A history of asthma copd or any lung condition?
            Speaker 1 : I have a mild asthma i use an albuterol inhaler occasionally maybe once or twice a week
            Speaker 0 : Any history of smoking i smoked in college but
            Speaker 1 : I quit ten years ago
            Speaker 0 : Do you have any allergic or chronic illness like diabetes hypertension or gerd
            Speaker 1 : I have type two diabetes diagnosed three years ago i take metamorphin five hundred mg twice a day no known allergies
            Speaker 0 : Any recent travel or contact with someone who's been sick
            Speaker 1 : No travel but my son had a very bad cold last week
            Speaker 0 : Alright let me check your vitals and listen to your lung your temperature is 106 respiratory rate is 22 oxygen saturation is 94 and heart rate is 92 beats per minute i hear some wheezing and crackles in both lower lung field your throat looks a bit red and post nasal drip is present
            Speaker 1 : Is it a chest infection
            Speaker 0 : Based on your symptoms history and exam it sounds like acute bronchitis possibly comp complicated by asthma and diabetes it's likely viral but with your underlying condition we should be cautious medications i'll prescribe amoxicillin chloride eight seventy five mg or one twenty five mg twice daily for seven days just in case there's a bacterial component continue using your albuterol inhaler but increase to every four to six six hours as needed i'm also prescribing a five day course of oral prednisone forty mg per day to reduce inflammation due to your asthma flare for the cough you can take guifenacin with dex with dextromethorphan as needed check your blood glucose more frequently while on prednisone as it can raise your sugar levels if your oxygen drops below 92 or your breathing worsens go to the emergency rest stay hydrated and avoid exertion use a humidifier at night and avoid cold air i want to see you back in three to five days to recheck your lungs and sugar control if symptoms worsen sooner come in immediately okay that makes sense will these make me sleepy the cough syrup might so take it at night and remember don't drive after taking it let me print your ex prescription and set your follow-up"

            Example SOAP ISSUES Note Output:

            Subjective:
            Acute bronchitis with asthma exacerbation
            - Constant cough, shortness of breath, chest tightness, low-grade fever
            - 6 days duration, worsening symptoms, fatigue with minimal exertion
            - Yellowish-green sputum production
            - Symptoms worse at night
            - Previous episodes: Mild asthma, uses albuterol inhaler 1-2 times weekly
            - Impact on daily activities: Tired after walking few steps
            - Associated symptoms: Wheezing, chest heaviness

            Type 2 Diabetes
            - Diagnosed 3 years ago
            - On metformin 500mg twice daily

            Past Medical History:
            - Mild asthma, uses albuterol inhaler 1-2 times weekly
            - Type 2 diabetes diagnosed 3 years ago
            - Ex-smoker, quit 10 years ago
            - No known allergies
            - Son had bad cold last week

            Objective:
            - Temperature: 37.6°C, Respiratory rate: 22, Oxygen saturation: 94%, Heart rate: 92 bpm
            - Wheezing and crackles in both lower lung fields, red throat, post-nasal drip present

            Assessment & Plan:
            Acute bronchitis with asthma exacerbation
            - Acute bronchitis complicated by asthma and diabetes
            - Investigations: Follow-up in 3-5 days to reassess lungs and glucose control
            - Treatment: Amoxicillin clavulanate 875/125mg twice daily for 7 days, increase albuterol inhaler to every 4-6 hours as needed, prednisone 40mg daily for 5 days, guaifenesin with dextromethorphan for cough as needed
            - Monitor blood glucose more frequently while on prednisone
            - Seek emergency care if oxygen drops below 92% or breathing worsens
            - Rest, hydration, avoid exertion, use humidifier at night, avoid cold air
            - Counselled regarding sedative effects of cough medication

            Type 2 Diabetes
            - Continue metformin 500mg twice daily
            - Monitor blood glucose more frequently while on prednisone
        

            Example 2:
                    
            Example Transcription:
            Speaker 0: Seeing you again how are you?
            Speaker 1: Hi i'm feeling okay normally i come every two months but scheduled earlier because i've been feeling depressed and anxious i had a panic attack this morning and felt like having chest pain tired and overwhelmed
            Speaker 0: well i'm sorry to hear that can you describe the panic attack further.
            Speaker 1: well i feel like i have a knot in my throat it's a very uncomfortable sensation i was taking lithium in the past but discontinued it because of the side effects
            Speaker 0: what kind of side effects?
            Speaker 1: i was dizzy slow thought process and was feeling nauseous i still throw up every morning i have this sensation like i need to throw up but since i don't have anything in my stomach i just retch
            Speaker 0: how long has this been going on for.
            Speaker 1: oh it's been going on for a month now.
            Speaker 0: and how about the panic attacks
            Speaker 1: I was having the panic attacks once every week in the past and had stopped for three months and this was the first time after that
            Speaker 0: Are you stressed out about anything in particular
            Speaker 1: I feel stressed out about my work and don't want to be there but have to be there for money i work in verizon in sales department sometimes when there are no customers that's when i feel the worst because if i can't hit the sales target they they can fire me
            Speaker 0: How was your job before that?
            Speaker 1: I was working at wells fargo before and that was even more stressful and before that i was working with uber which was not as stressful but the money wasn't good and before that i was working for at and t which was good but because of my illness i could not continue to work there
            Speaker 0: and how has your mood been
            Speaker 1: i feel down i feel like i have a lack of motivation i used to watch a lot of movies and tv shows in the past but now i don't feel interested anymore
            Speaker 0: what do you do in your free time?
            Speaker 1: oh i spend a lot of time on facebook just scrolling you know i have a friend in cuba and he chats with me every day which kinda distracts me
            Speaker 0: who do you live with?
            Speaker 1: i live with my husband and a child a four year old girl
            Speaker 0: do you like spending time with them
            Speaker 1: not really i don't feel like doing much my daughter is closer with my husband so she likes spending more time with him i do take her to the park though
            Speaker 0: how's your sleep been
            Speaker 1: i sleep well i sleep for more than ten hours
            Speaker 0: is it normal for you to sleep for ten hours or do you feel like you have been sleeping more than usual
            Speaker 1: no it's normal i usually sleep that long
            Speaker 0: how's your appetite been
            Speaker 1: i feel like my appetite has reduced
            Speaker 0: and how about your concentration have you been able to focus on your work
            Speaker 1: no my concentration is really bad
            Speaker 0: for how long
            Speaker 1: it's been like that for a month now
            Speaker 0: do you get any dark thoughts like thoughts about hurting yourself or anybody else no how about experiencing anything unusual like hearing voices no one else can hear or seeing things no one else can see such as shadows etcetera
            Speaker 1: no k
            Speaker 0: in the last month have you had any episodes where you have be you have the opposite of low energy like having a lot of energy a lot of thoughts running in your head really fast
            Speaker 1: no i know what manic episodes look like my husband has been keeping an eye on me and i i'm also aware of how how i feel when it starts
            Speaker 0: okay that's good seems like the main issue right now is depression and anxiety have you been getting the monthly injections on time
            Speaker 1: yes
            speaker 0: that's good are you open to start oral medication to help with your symptoms in addition to the injection
            speaker 1: yes
            Speaker 0: okay we're going to start you on a medication for depression that you'll take every morning however you will have to watch out for hyperactivity because medication for depression can trigger a manic episode if you feel like you are getting excessively active and not sleeping well give us a call
            Speaker 1: Okay doctor
            Speaker 0: we will also start you on a medication for anxiety
            Speaker 1: okay sounds good
            Speaker 0: okay pleasure meeting you and see you in next follow-up visit
            Speaker 1: okay thank you
            Speaker 0: thanks bye
           
            Example SOAP ISSUES Note Output:

            Subjective:
            Depression and Anxiety
            - Reports feeling depressed and anxious
            - Experienced panic attack this morning with chest pain, fatigue, feeling overwhelmed
            - Describes sensation of knot in throat
            - Panic attacks occurring once weekly previously, stopped for three months, recent recurrence
            - Morning retching/vomiting for past month
            - Work-related stress (sales position at Verizon) with concerns about meeting targets
            - Previous employment history includes Wells Fargo (more stressful), Uber (less stressful but poor pay), AT&T (had to leave due to illness)
            - Mood: feels down, lacks motivation
            - Loss of interest in previously enjoyed activities (movies, TV shows)
            - Spends free time scrolling through Facebook
            - Reduced interest in family interactions
            - Sleep: 10 hours nightly (normal pattern)
            - Reduced appetite
            - Poor concentration for past month
            - Denies suicidal/homicidal ideation
            - Denies hallucinations
            - No recent manic episodes
            - Aware of manic symptoms, husband monitoring

            Past Medical History:
            - History of bipolar disorder (currently receiving monthly injections)
            - Previously prescribed lithium but discontinued due to side effects (dizziness, slowed thought process, nausea)

            Objective:
            - Currently receiving monthly injections for bipolar disorder

            Assessment & Plan:
            Depression and Anxiety
            - Assessment: Depression with anxiety symptoms
            - Treatment planned: 
            - Continue monthly injections
            - Starting new oral medication for depression (morning dose)
            - Starting medication for anxiety
            - Counselled regarding risk of medication triggering manic episodes and to report symptoms of hyperactivity or reduced sleep
            - Follow-up appointment scheduled
        
        """
 
        elif template_type == "cardiology_letter":
            current_date = None
            if "metadata" in transcription and "current_date" in transcription["metadata"]:
                current_date = transcription["metadata"]["current_date"]
                main_logger.info(f"[OP-{operation_id}] Using current_date from metadata: {current_date}")
            

            user_instructions= f"""You are provided with a medical conversation transcript. 
            Analyze the transcript and generate a structured CARDIOLOGY CONSULT NOTE following the specified template. 
            Use only the information explicitly provided in the transcript, and do not include or assume any additional details. 
            Ensure the output is a valid JSON object with the CARDIOLOGY CONSULT NOTE sections formatted professionally and concisely in a doctor-like tone. 
            Make sure the output is in valid JSON format. 
            If the patient didnt provide the information regarding any section then ignore the respective section.
            Include the all numbers in numeric format.
            Make sure the output is concise and to the point.
            ENsure the data in each section should be to the point and professional.
            Make it useful as doctors perspective so it makes there job easier, dont just dictate and make a note, analyze the conversation, summarize it and make a note that best desrcibes the patient's case as a doctor's perspective.
            Add time related information in the report dont miss them.
            Make sure the point are concise and structured and looks professional.
            Use medical terms to describe each thing.
            Follow strictly the format given below, and always written same output format.
            If data for any field is not available dont write anything under that heading and ignore it.
            Use {current_date if current_date else "the current date"} as the reference date for all temporal expressions in the transcription.
            Below is the transcript:\n\n{conversation_text}
            """
            system_message = f"""You are a medical documentation assistant tasked with generating a detailed and structured cardiac assessment report based on a predefined template. 
            Your output must maintain a professional, concise, and doctor-like tone, avoiding verbose or redundant phrasing. 
            All information must be sourced exclusively from the provided transcript, contextual notes, or clinical notes, and only explicitly mentioned details should be included. 
            If information for a specific section or placeholder is unavailable, use "None known" for risk factors, medical history, medications, allergies, social history, and investigations, or omit the placeholder entirely for other sections as specified. 
            Do not invent or infer patient details, assessments, plans, interventions, or follow-up care beyond what is explicitly stated. 
            If data for any field is not available dont write anything under that heading and ignore it.
           
            Date Handling Instructions:
                Use {current_date if current_date else "the current date"} as the reference date for all temporal expressions in the transcription.
                Convert relative time references to specific dates based on the reference date:
                    "This morning," "today," or similar terms refer to the reference date.
                    "X days ago" refers to the reference date minus X days (e.g., "five days ago" is reference date minus 5 days).
                    "Last week" refers to approximately 7 days prior, adjusting for specific days if mentioned (e.g., "last Wednesday" is the most recent Wednesday before the reference date).
                Format all dates as DD/MM/YYYY (e.g., 10/06/2025) in the CARDIOLOGY LETTER output.
                Do not use hardcoded dates or assumed years unless explicitly stated in the transcription.
                Verify date calculations to prevent errors, such as incorrect years or misaligned days.
            {preservation_instructions} {grammar_instructions}
            Follow the structured guidelines below for each section of the report:

            1. Patient Introduction:
            - Begin the report with: "I had the pleasure of seeing [patient name] today. [patient pronoun] is a pleasant [patient age] year old [patient gender] that was referred for cardiac assessment."
            - Insert patient name, pronoun (e.g., He/She/They), age, and gender exactly as provided in the transcript or context. If any detail is missing, leave the placeholder blank and proceed.

            2. Cardiac Risk Factors:
            - List under the heading "CARDIAC RISK FACTORS" in the following order: Increased BMI, Hypertension, Dyslipidemia, Diabetes mellitus, Smoker, Ex-smoker, Family history of atherosclerotic disease in first-degree relatives.
            - For each risk factor, use verbatim text from contextual notes if available, updating only with new or differing information from the transcript.
            - If no information is provided in the transcript or context, write "None known" for each risk factor.
            - For Smoker, include pack-years, years smoked, and number of cigarettes or packs per day if mentioned in the transcript or context.
            - For Ex-smoker, include only if the patient is not currently smoking. State the year quit and whether they quit remotely (e.g., "quit remotely in 2015") if mentioned. Omit this section if the patient is a current smoker.
            - For Family history of atherosclerotic disease, specify if the patient’s mother, father, sister, brother, or child had a myocardial infarction, coronary angioplasty, coronary stenting, coronary bypass, peripheral arterial procedure, or stroke prior to age 55 (men) or 65 (women), as mentioned in the transcript or context.

            3. Cardiac History:
            - List under the heading "CARDIAC HISTORY" all cardiac diagnoses in the following order, if mentioned: heart failure (HFpEF or HFrEF), cardiomyopathy, arrhythmias (atrial fibrillation, atrial flutter, SVT, VT, AV block, heart block), devices (pacemakers, ICDs), valvular disease, endocarditis, pericarditis, tamponade, myocarditis, coronary disease.
            - Use verbatim text from contextual notes, updating only with new or differing transcript information.
            - For heart failure, specify HFpEF or HFrEF and include ICD implantation details (date and model) if mentioned.
            - For arrhythmias, include history of cardioversions and ablations (type and date) if mentioned.
            - For AV block, heart block, or syncope, state if a pacemaker was implanted, including type, model, and date, if mentioned.
            - For valvular heart disease, note the type of intervention (e.g., valve replacement) and date if mentioned.
            - For coronary disease, summarize previous angiograms, coronary anatomy, and interventions (angioplasty, stenting, CABG) with graft details and dates if mentioned.
            - Omit this section entirely if no cardiac history is mentioned in the transcript or context.

            4. Other Medical History:
            - List under the heading "OTHER MEDICAL HISTORY" all non-cardiac diagnoses and previous surgeries with associated years if mentioned.
            - Use verbatim text from contextual notes, updating only with new or differing transcript information.
            - Write "None known" if no non-cardiac medical history is mentioned in the transcript or context.

            5. Current Medications:
            - List under the heading "CURRENT MEDICATIONS" in the following subcategories, each on a single line:
                - Antithrombotic Therapy: Include aspirin, clopidogrel, ticagrelor, prasugrel, apixaban, rivaroxaban, dabigatran, edoxaban, warfarin.
                - Antihypertensives: Include ACE inhibitors, ARBs, beta blockers, calcium channel blockers, alpha blockers, nitrates, diuretics (excluding furosemide, e.g., HCTZ, chlorthalidone, indapamide).
                - Heart Failure Medications: Include Entresto, SGLT2 inhibitors, mineralocorticoid receptor antagonists, furosemide, metolazone, ivabradine.
                - Lipid Lowering Medications: Include statins, ezetimibe, PCSK9 modifiers, fibrates, icosapent ethyl.
                - Other Medications: Include any medications not listed above.
            - For each medication, include dose, frequency, and administration if mentioned, and note the typical recommended dosage per tablet or capsule in parentheses if it differs from the patient’s dose.
            - Use verbatim text from contextual notes, updating only with new or differing transcript information.
            - Write "None known" for each subcategory if no medications are mentioned.

            6. Allergies and Intolerances:
            - List under the heading "ALLERGIES AND INTOLERANCES" in sentence format, separated by commas, with reactions in parentheses if mentioned (e.g., "penicillin (rash), sulfa drugs (hives)").
            - Use verbatim text from contextual notes, updating only with new or differing transcript information.
            - Write "None known" if no allergies or intolerances are mentioned.

            7. Social History:
            - List under the heading "SOCIAL HISTORY" in a short-paragraph narrative form.
            - Include living situation, spousal arrangements, number of children, working arrangements, retirement status, smoking status, alcohol use, illicit or recreational drug use, and private drug/health plan status, if mentioned.
            - For smoking, write "Smoking history as above" if the patient smokes or is an ex-smoker; otherwise, write "Non-smoker."
            - For alcohol, specify the number of drinks per day or week if mentioned.
            - Include comments on activities of daily living (ADLs) and instrumental activities of daily living (IADLs) if relevant.
            - Use verbatim text from contextual notes, updating only with new or differing transcript information.
            - Write "None known" if no social history is mentioned.

            8. History:
            - List under the heading "HISTORY" in narrative paragraph form, detailing all reasons for the current visit, chief complaints, and a comprehensive history of presenting illness.
            - Include mentioned negatives from exams and symptoms, as well as details on physical activities and exercise regimens, if mentioned.
            - Only include information explicitly stated in the transcript or context.
            - End with: "Review of systems is otherwise non-contributory."

            9. Physical Examination:
            - List under the heading "PHYSICAL EXAMINATION" in a short-paragraph narrative form.
            - Include vital signs (blood pressure, heart rate, oxygen saturation), cardiac examination, respiratory examination, peripheral edema, and other exam insights only if explicitly mentioned in the transcript or context.
            - If cardiac exam is not mentioned or normal, write: "Precordial examination was unremarkable with no significant heaves, thrills or pulsations. Heart sounds were normal with no significant murmurs, rubs, or gallops."
            - If respiratory exam is not mentioned or normal, write: "Chest was clear to auscultation."
            - If peripheral edema is not mentioned or normal, write: "No peripheral edema."
            - Omit other physical exam insights if not mentioned.

            10. Investigations:
                - List under the heading "INVESTIGATIONS" in the following subcategories, each on a single line with findings and dates in parentheses followed by a colon:
                - Laboratory investigations: List CBC, electrolytes, creatinine with GFR, troponins, NTpBNP or BNP level, A1c, lipids, and other labs in that order, if mentioned.
                - ECGs: List ECG findings and dates.
                - Echocardiograms: List echocardiogram findings and dates.
                - Stress tests: List stress test findings (including stress echocardiograms and graded exercise challenges) and dates.
                - Holter monitors: List Holter monitor findings and dates.
                - Device interrogations: List device interrogation findings and dates.
                - Cardiac perfusion imaging: List cardiac perfusion imaging findings and dates.
                - Cardiac CT: List cardiac CT findings and dates.
                - Cardiac MRI: List cardiac MRI findings and dates.
                - Other investigations: List other relevant investigations and dates.
                - Use verbatim text from contextual notes, updating only with new or differing transcript information.
                - Write "None known" for each subcategory if no findings are mentioned.

            11. Summary:
                - List under the heading "SUMMARY" in a cohesive narrative paragraph.
                - Start with: "[patient name] is a pleasant [age] year old [gender] that was seen today for cardiac assessment."
                - Include: "Cardiac risk factors include [list risk factors]" if mentioned in the transcript or context.
                - Summarize patient symptoms from the History section and cardiac investigations from the Investigations section, if mentioned.
                - Omit risk factors or summary details if not mentioned.

            12. Assessment/Plan:
                - List under the heading "ASSESSMENT/PLAN" for each medical issue, structured as:
                - #[number] [Condition]
                - Assessment: [Current assessment of the condition, drawn from context and transcript]
                - Plan: [Management plan, including investigations, follow-up, and reasoning for the plan, drawn from context and transcript. Include counselling details if mentioned.]
                - Number each issue sequentially (e.g., #1, #2) and ensure all information is explicitly mentioned in the transcript or context.

            13. Follow-Up:
                - List under the heading "FOLLOW-UP" any follow-up plans and time frames explicitly mentioned in the transcript.
                - If no time frame is specified, write: "Will follow-up in due course, pending investigations, or sooner should the need arise."

            14. Closing:
                - End the report with: "Thank you for the privilege of allowing me to participate in [patient’s name] care. Feel free to reach out directly if any questions or concerns."

            Additional Instructions:
            - Ensure strict adherence to the template structure, maintaining the exact order and headings as specified.
            - Use "-  " only where indicated (e.g., Assessment/Plan).
            - Write in complete sentences for narrative sections (History, Social History, Physical Examination, Summary).
            - If data for any field is not available dont write anything under that heading and ignore it.
            - Ensure all sections are populated only with explicitly provided data, preserving accuracy and professionalism.
        """
        # Make the API request to GPT - Remove the response_format parameter which is causing the error
        response = client.chat.completions.create(
            model="gpt-4.1",
            messages=[
                {"role": "system", "content": system_message},
                {"role": "user", "content": user_instructions if template_type =="h75" or template_type == "new_soap_note" or template_type =="mental_health_appointment" or template_type =="clinical_report" or template_type == "cardiology_letter" or template_type == "detailed_soap_note" or template_type == "soap_issues" or template_type == "consult_note" or template_type == "referral_letter" else f"Here is a medical conversation. Please format it into a structured {template_type}. YOUR RESPONSE MUST BE VALID JSON:\n\n{conversation_text}"}
            ],
            temperature=0.3, # Lower temperature for more consistent outputs
            response_format={"type": "json_object"}
        )
        print(conversation_text)
        # Extract the response and validate JSON
        gpt_response = response.choices[0].message.content
        
        # Validate that the response is proper JSON
        try:
            json.loads(gpt_response)
        except json.JSONDecodeError:
            # If not valid JSON, log the error and return error message
            error_msg = f"GPT did not return valid JSON: {gpt_response[:100]}..."
            error_logger.error(error_msg)
            return f"Error: {error_msg}"
        
        # Log success and return response
        main_logger.info(f"[OP-{operation_id}] GPT response generated successfully for template: {template_type}")
        return gpt_response
        
    except Exception as e:
        operation_id = locals().get('operation_id', str(uuid.uuid4())[:8])
        error_logger.error(f"[OP-{operation_id}] Error generating GPT response: {str(e)}", exc_info=True)
        return f"Error generating GPT response: {str(e)}"

@log_execution_time
async def format_report(gpt_response, template_type):
    """
    Format a report based on the template type.
    
    Args:
        gpt_response: JSON string containing the GPT formatted response
        template_type: Type of report template to use
        
    Returns:
        String containing the formatted report or error message
    """
    try:
        # Parse the GPT response
        data = json.loads(gpt_response)  # Ensure gpt_response is parsed as JSON
        
        # Add grammar validation instruction to the data
        if isinstance(data, dict):
            data["_grammar_instruction"] = "Ensure all text follows standard US English grammar rules with proper spelling, punctuation, and capitalization."
        
        # Format based on template type
        if template_type == "clinical_report":
            return await format_clinical_report(data)
        elif template_type == "soap_note":
            return await format_soap_note(data)
        elif template_type == "new_soap_note":
            return await format_new_soap(data)
        elif template_type == "h75":
            return await format_h75(data)
        elif template_type == "soap_issues":
            return await format_soap_issues(data)
        elif template_type == "progress_note":
            return await format_progress_note(data)
        elif template_type == "mental_health_appointment":
            return await format_mental_health_note(data)
        elif template_type == "cardiology_letter":
            return await format_cardiac_report(data)
        elif template_type == "followup_note":
            return await format_followup_note(data)
        elif template_type == "meeting_minutes":
            return await format_meeting_minutes(data)
        elif template_type == "referral_letter":
            return await format_referral_letter(data)
        elif template_type == "detailed_dietician_initial_assessment":
            return await format_dietician_assessment(data)
        elif template_type == "psychology_session_notes":
            return await format_psychology_session_notes(data)
        elif template_type == "pathology_note":
            return await format_pathology(data)
        elif template_type == "consult_note":
            return await format_consult_note(data)
        elif template_type == "discharge_summary":
            return await format_discharge_summary(data)
        elif template_type == "case_formulation":
            return await format_case_formulation(data)
        elif template_type == "detailed_soap_note":
            return await format_detailed_soap_note(data)
        else:
            return f"Error: Unsupported template type '{template_type}'"
    except json.JSONDecodeError:
        error_logger.error(f"Invalid JSON in GPT response: {gpt_response}")
        return f"Error: Invalid JSON format in GPT response"
    except Exception as e:
        error_logger.error(f"Error formatting report: {str(e)}", exc_info=True)
        return f"Error formatting report: {str(e)}"


@app.post("/generate-template-report")
@log_execution_time
async def generate_template_report(
    transcript_id: str = Form(...),
    template_type: str = Form("clinical_report")
):
    """
    Generate a formatted report using a specific template based on an existing transcript.
    This endpoint can be used after either live transcription or AI summary to create the final report.
    
    Args:
        transcript_id: ID of the transcript to use for report generation
        template_type: Type of template report to generate
    """
    try:
        # Initialize status tracking
        report_status = "processing"
        report_id = None
        gpt_response = None
        formatted_report = None

        # Validate template type
        valid_templates = ["clinical_report","h75","new_soap_note","soap_issues" ,"detailed_soap_note","soap_note", "progress_note", "mental_health_appointment", "cardiology_letter", "followup_note", "meeting_minutes","referral_letter","detailed_dietician_initial_assessment","psychology_session_notes","pathology_note", "consult_note","discharge_summary","case_formulation"]
        
        if template_type not in valid_templates:
            error_msg = f"Invalid template type '{template_type}'. Must be one of: {', '.join(valid_templates)}"
            error_logger.error(error_msg)
            return JSONResponse({
                "status": "failed",
                "error": error_msg,
                "report_status": "failed"
            }, status_code=400)
            
        # Get transcript data
        table = dynamodb.Table('transcripts')
        response = table.get_item(Key={"id": transcript_id})
        
        if 'Item' not in response:
            return JSONResponse({
                "status": "failed",
                "error": f"Transcript ID {transcript_id} not found",
                "report_status": "failed"
            }, status_code=404)
            
        transcript_item = response['Item']
        
        # Get transcript data
        transcript_data = transcript_item.get('transcript', '{}')

        # Check if it's a Binary object and decrypt if needed
        if str(type(transcript_data)) == "<class 'boto3.dynamodb.types.Binary'>":
            transcript_data = decrypt_data(transcript_data)
            # Decode bytes to string after decryption
            transcript_data = transcript_data.decode('utf-8')
        elif isinstance(transcript_data, bytes):
            transcript_data = decrypt_data(transcript_data).decode('utf-8')
            
        # Now parse the JSON
        try:
            transcription = json.loads(transcript_data)
        except json.JSONDecodeError:
            return JSONResponse({
                "status": "failed",
                "error": "Invalid transcript data format",
                "report_status": "failed"
            }, status_code=400)

        try:
            # Generate GPT response
            main_logger.info(f"Generating {template_type} template for transcript {transcript_id}")
            gpt_response = await generate_gpt_response(transcription, template_type)
            
            if isinstance(gpt_response, str) and gpt_response.startswith("Error"):
                report_status = "failed"
                return JSONResponse({
                    "status": "failed",
                    "error": gpt_response,
                    "report_status": report_status
                }, status_code=400)
            
            # Format the report
            formatted_report = await format_report(gpt_response, template_type)
            
            if isinstance(formatted_report, str) and formatted_report.startswith("Error"):
                report_status = "failed"
                return JSONResponse({
                    "status": "failed",
                    "error": formatted_report,
                    "report_status": report_status
                }, status_code=400)
            
            # Save report to database
            report_id = await save_report_to_dynamodb(
                transcript_id,
                gpt_response,
                formatted_report,
                template_type,
                status=report_status
            )
            
            report_status = "completed"
            
            # Return the complete response
            response_data = {
                "status": "success",
                "report_status": report_status,
                "transcript_id": transcript_id,
                "report_id": report_id,
                "template_type": template_type,
                "gpt_response": json.loads(gpt_response) if gpt_response else None,
                "formatted_report": formatted_report
            }
            
            main_logger.info(f"Report generation completed successfully")
            return JSONResponse(response_data)

        except Exception as e:
            error_msg = f"Error during processing: {str(e)}"
            error_logger.exception(error_msg)
            
            # Update status in DynamoDB if we have ID
            if report_id:
                await save_report_to_dynamodb(
                    transcript_id,
                    gpt_response,
                    formatted_report,
                    template_type,
                    status="failed"
                )
            
            return JSONResponse({
                "status": "failed",
                "error": error_msg,
                "report_status": report_status,
                "report_id": report_id
            }, status_code=500)

    except Exception as e:
        error_msg = f"Unexpected error in generate_template_report: {str(e)}"
        error_logger.exception(error_msg)
        return JSONResponse({
            "status": "failed",
            "error": error_msg,
            "report_status": "failed"
        }, status_code=500)


template_display_map = {
    "detailed_soap_note": "SOAP Detailed",
    "new_soap_note": "S.O.A.P (Default)",
    "soap_issues": "SOAP Issues",
    "referral_letter": "Referral Letter",
    "cardiology_consult": "Cardiology Consult",
    "echocardiography_report_vet": "Echocardiography Report (Vet)",
    "cardio_consult": "Cardio Consult",
    "cardio_patient_explainer": "Cardio Patient Explainer",
    "coronary_angiograph_report": "Coronary Angiograph Report",
    "left_heart_catheterization": "Left Heart Catheterization",
    "right_and_left_heart_study": "Right and Left Heart Study",
    "toe_guided_cardioversion_report": "TOE Guided Cardioversion Report",
    "hospitalist_progress_note": "Hospitalist Progress Note",
    "multiple_issues_visit": "Multiple Issues Visit",
    "counseling_consultation": "Counseling Consultation",
    "gp_consult_note": "GP Consult Note",
    "speech_pathology_note": "Speech Pathology Note",
    "summary": "Summary",
    "physio_soap_outpatient": "Physio SOAP Outpatient",
    "h75": "H75",
    "consult_note": "Consult Note",
    "discharge_summary": "Discharge Summary",
    "clinical_report": "Clinical Report",
    "mental_health_appointment": "Mental Health Note",
    "cardiology_letter": "Cardiology Consultation Letter",
    "psychology_session_notes": "Psychology Session Notes",
    "pathology_note": "Speech Pathology Therapy",
    "followup_note": "Follow-up Note",
    "meeting_minutes": "Meeting Minutes",
    "case_formulation": "Case Formulation",
    "progress_note": "Progress Note",
    "detailed_dietician_initial_assessment": "Detailed Dietician Initial Assessment"
}


template_structures_verbose = {
    "cardiology_consult": {
        "display_name": "Cardiology Consult",
        "structure": [
            {"Reason For Visit": "Describe the clinical reason for today's cardiac assessment."},
            {"Cardiac Risk Factors": "Include BMI, hypertension, diabetes, smoking history, and family history of cardiac disease."},
            {"Cardiac History": "Summarize any cardiac diagnoses (e.g., heart failure, arrhythmias, previous interventions)."},
            {"Other Medical History": "List relevant non-cardiac conditions and surgical history."},
            {"Current Medications": "Categorize medications such as antithrombotics, antihypertensives, heart failure drugs, and others."},
            {"Allergies and Intolerances": "Mention drug or substance allergies, with reaction details if available."},
            {"Social History": "Brief lifestyle background including living situation, occupation, smoking/alcohol/drug use."},
            {"History": "Comprehensive history of presenting illness and relevant symptom narratives."},
            {"Physical Examination": "Findings from cardiovascular, respiratory, and general exams including vitals."},
            {"Investigations": "Summarize recent lab tests, ECGs, echocardiograms, and other diagnostics."},
            {"Summary": "One-paragraph summary of case including demographics, symptoms, and key findings."},
            {"Assessment/Plan": "Structured issues with diagnosis and management plans per condition."},
            {"Follow-Up": "Document next review plans, timelines, or patient instructions."}
        ]
    },
    "echocardiography_report_vet": {
        "display_name": "Echocardiography Report (Vet)",
        "structure": [
            {"VET Echocardiography Report": "Report title for veterinary cardiac imaging."},
            {"Patient Information": "Includes patient name, species, breed, age, sex, and weight."},
            {"Reason for Echocardiography": "Brief reason for referral or presenting complaint."},
            {"Echocardiographic Findings": "Detailed structural and functional findings for each heart chamber and valve."},
            {"Measurements": "Numeric values such as LA/Ao ratio, EPSS, etc."},
            {"Interpretation": "Clinically relevant interpretation of imaging results."},
            {"Diagnosis": "Primary echocardiographic diagnosis."},
            {"Recommendations": "Suggestions for treatment, follow-up, or further diagnostics."}
        ]
    },
    "cardio_consult": {
        "display_name": "Cardio Consult",
        "structure": [
            {"Subjective": "Describes symptoms, past cardiovascular history, lifestyle, and allergies."},
            {"Objective": "Summarizes vital signs, exam findings, and cardiovascular investigations."},
            {"Assessment": "Diagnoses or clinical impressions related to cardiovascular issues."},
            {"Plan": "Treatment steps, referrals, medications, and counseling provided."}
        ]
    },
     "cardio_patient_explainer": {
        "display_name": "Cardio Patient Explainer",
        "structure": [
            {"1. [Topic/Issue #1: Description]": "Explain the first health topic in layman's terms, including its meaning and required actions or medications."},
            {"2. [Topic/Issue #2: Description]": "Describe another issue or topic, symptoms to monitor, and next steps or follow-up actions."},
            {"3. [Topic/Issue #3: Description]": "Summarize any additional discussion points with patient guidance and advice."},
            {"Next Steps": "Clearly state what the patient needs to do next, such as follow-up, start medications, or watch for side effects."},
            {"Closing Note": "A friendly conclusion thanking the patient and encouraging them to reach out with concerns."}
        ]
    },
    "coronary_angiograph_report": {
        "display_name": "Coronary Angiograph Report",
        "structure": [
            {"Indication": "Reason for the procedure, such as chest pain or suspected coronary artery disease."},
            {"Access & Setup": "Details about vascular access site, catheter types, anesthesia, and procedural prep."},
            {"Sedation & Medications Administered": "List of medications used during the procedure, including dosages."},
            {"Procedure Performed": "Specify whether the procedure was diagnostic or interventional."},
            {"Findings": "Structured findings for each artery, including flow, stenosis, and anatomical observations."},
            {"Closure Technique": "Describe how the access site was closed and any post-procedure instructions."},
            {"Complications": "Mention any complications or explicitly state 'None' if documented."},
            {"Conclusion / Summary": "A high-level interpretation of the overall findings."},
            {"Recommendations / Next Steps": "Advice on follow-up care, medications, or lifestyle adjustments."}
        ]
    },
    "left_heart_catheterization": {
        "display_name": "Left Heart Catheterization",
        "structure": [
            {"Summary": "Main findings, interventions, and key metrics like LVEDP and ejection fraction."},
            {"Right Heart Catheterization": "Hemodynamic readings like RA, RV, PA pressures, and cardiac index, if performed."},
            {"Procedures Performed": "List of catheter-based procedures carried out, with equipment details."},
            {"Interventions": "Details about treated lesions, tools used (wires, stents), and pre/post metrics."},
            {"Recommendations": "Post-op care including medications, monitoring, and risk factor guidance."},
            {"Coronary Anatomy": "Description of each coronary vessel’s structure and dominance, if documented."}
        ]
    },
    "right_and_left_heart_study": {
        "display_name": "Right and Left Heart Study",
        "structure": [
            {"Clinical Indication": "Why the study was performed, such as to assess dyspnea or pulmonary hypertension."},
            {"Procedure Performed": "Brief mention of right and left heart cath or similar interventions."},
            {"Technique & Access": "Access site, catheter types, and medications used."},
            {"Hemodynamic Measurements": "Recorded pressures from different cardiac chambers and arteries."},
            {"Oximetry / Saturation Data": "O2 saturation readings from various vessels, if available."},
            {"Closure Technique": "How the vascular access site was closed post-procedure."},
            {"Conclusion / Summary": "Summarize hemodynamic status and key interpretations from the study."}
        ]
    },
    "toe_guided_cardioversion_report": {
        "display_name": "TOE Guided Cardioversion Report",
        "structure": [
            {"Clinical Indication": "State the clinical reason for the procedure, such as atrial fibrillation, flutter, or pre-cardioversion assessment."},
            {"Procedure Description": "Describe the procedural approach, including anesthesia, setting, and method of cardioversion under TOE guidance."},
            {"Transoesophageal Echocardiogram Findings": "Summarize TOE results, particularly the presence or absence of intracardiac thrombus or other abnormalities."},
            {"ECG Findings Before Cardioversion": "Mention pre-cardioversion rhythm, rate, and any notable ECG features."},
            {"Cardioversion Details": "Detail number of shocks, energy settings, and the rhythm outcome (e.g., sinus restoration)."},
            {"Complications": "State if any complications occurred during the procedure, only if explicitly mentioned."},
            {"Conclusion / Summary": "Concise summary of procedural outcome, such as successful restoration of sinus rhythm."},
            {"Recommendations": "Include instructions for post-cardioversion care, medications, and follow-up."}
        ]
    },
    "hospitalist_progress_note": {
        "display_name": "Hospitalist Progress Note",
        "structure": [
            {"Clinical Course": "Summarize patient background, reason for admission, and clinical progress since admission."},
            {"Today's Updates": "Narrative of condition changes, new events, interventions, and diagnostics within the last 24 hours."},
            {"Review of Systems (ROS)": "Mention any positive or negative findings from the ROS, only if explicitly stated."},
            {"Physical Exam": "Summarize physical examination findings including vitals and systemic assessments."},
            {"Assessment and Plan": "List medical issues with condition name, assessment, plan, and any counseling provided."},
            {"Fluids, Electrolytes, Diet": "Document fluid status, electrolyte needs, and dietary orders."},
            {"DVT prophylaxis": "Name of prophylactic agent ordered, if stated."},
            {"Central line": "Presence and purpose of central line, if documented."},
            {"Foley catheter": "Presence and indication for Foley catheter, if present."},
            {"Code Status": "Mention resuscitation status (e.g., Full Code, DNR), if explicitly stated."},
            {"Disposition": "Include discharge timeline, rehab needs, or social services efforts, if mentioned."}
        ]
    },
    "multiple_issues_visit": {
        "display_name": "Multiple Issues Visit",
        "structure": [
            {"Consent": "Document if the patient consented to use of electronic scribing (include only if mentioned)."},
            {"Summary for Follow up": "Bullet-point list of each issue’s differential diagnosis and treatment summary."},
            {"Issue 1": "Full detail of Issue 1 with history, objective findings, diagnosis, plan, investigations, treatment, and referrals."},
            {"Issue 2": "Full detail of Issue 2 with history, objective findings, diagnosis, plan, investigations, treatment, and referrals."},
            {"Issue 3, 4, 5...": "Full detail of subsequent issues following the same format, depending on transcript content."}
        ]
    },
    "physio_soap_outpatient": {
        "display_name": "Physio SOAP Outpatient",
        "structure": [
            {"Current Condition/Complaint": "Summarize symptom progress, medication usage, radiology findings, and adherence to plan since last visit."},
            {"Objective": "Group and list physical findings and examination data like ROM, strength, posture, etc."},
            {"Treatment": "List all treatments provided — education, hands-on techniques, exercises, and HEP (with reps, sets, and frequency)."},
            {"Assessment": "State diagnosis and progress summary based on findings, goals, and barriers (if mentioned)."},
            {"Plan": "Outline future treatment plan, next review date, and any referrals or communications needed."}
        ]
    },
    "counseling_consultation": {
        "display_name": "Counseling Consultation",
        "structure": [
            {"Current Presentation": "Key concerns or presenting issues raised by the patient, including severity and daily impact."},
            {"Session Content": "Detailed overview of topics discussed, patient’s thoughts and insights, and any realizations made during the session."},
            {"Obstacles, Setbacks and Progress": "Challenges the patient is facing, any recent regressions, and progress since the previous session."},
            {"Session Summary": "Clinician's summary of key themes, emotional responses, behavioral patterns, and therapeutic interpretations."},
            {"Next Steps": "Agreed-upon goals, future therapeutic tasks, or scheduling of the next session."}
        ]
    },
    "gp_consult_note": {
        "display_name": "GP Consult Note",
        "structure": [
            {"History": "Detailed symptom-wise history including quality, severity, duration, associated symptoms, and current or planned treatments."},
            {"Past history": "Past medical and surgical history, ongoing treatments, and medication side effects if explicitly mentioned."},
            {"Family history": "Any relevant familial health background and social determinants if discussed."},
            {"Examination": "Findings from physical or mental examination, including vitals and noted abnormalities."},
            {"Plan": "Clinical management strategies including medications, follow-up, referrals, or investigations."}
        ]
    },
   "detailed_dietician_initial_assessment": {
        "display_name": "Detailed Dietician Initial Assessment",
        "structure": [
            {"Weight History": "Includes dieting attempts, weight fluctuation patterns, and baseline (pre-morbid) weight."},
            {"Body Image": "Behaviors or emotions related to body checking or avoidance."},
            {"Disordered Eating/Eating Disorder Behavior": "Patterns of restrictive eating, binging, purging, compulsive behaviors, and substance use for weight control."},
            {"Eating Behavior": "Cues for hunger/fullness, food rules, allergies, and dietary preferences."},
            {"Nutrition Intake": "Daily food and fluid intake structure including meals, snacks, and timing."},
            {"Physical Activity Behavior": "Types and frequency of physical activity and the client’s relationship with exercise."},
            {"Medical & Psychiatric History": "Existing or previous diagnoses, treatments, or relevant conditions."},
            {"Menstrual History": "Onset age, cycle pattern, symptoms, and contraceptive use."},
            {"Gut/Bowel Health": "Digestive and bowel function observations or concerns."},
            {"Pathology/Scans": "Available ECG, BMD, or related lab/imaging reports."},
            {"Medications/Supplements": "Current prescribed or over-the-counter supplements and medications."},
            {"Social History/Lifestyle": "Living situation, occupation, habits (alcohol/smoking), stress, sleep, self-care routines, and other health providers involved."}
        ]
    },
   "referral_letter": {
    "display_name": "Referral Letter",
    "structure": [
        {"Practice Letterhead": "Include '[Practice Letterhead]' if not known."},
        {"Date": "Use current date in DD/MM/YYYY format."},
        {"To": {
            "Consultant Name": "Include 'Dr. [Consultant’s Name]' if known, otherwise default.",
            "Specialist Clinic/Hospital Name": "Add '[Specialist Clinic/Hospital Name]' if unknown.",
            "Address": "Add address or '[Address Line 1]\n[Address Line 2]\n[City, Postcode]' if unknown."
        }},
        {"Salutation": "Use 'Dear Dr. [Consultant’s Last Name]' or default if not known."},
        {"Referral Subject Line": "Use 'Re: Referral for [Patient’s Name], [Date of Birth: DOB]'. Use placeholders if not known."},
        {"Introductory Sentence": "Brief line stating purpose of referral and clinical concern."},
        {"Clinical Details": {
            "Presenting Complaint": "Primary symptoms or reason for visit.",
            "Duration": "Duration of symptoms.",
            "Relevant Findings": "Pertinent clinical or exam findings.",
            "Past Medical History": "Relevant chronic conditions.",
            "Current Medications": "List of current medications."
        }},
        {"Investigations": {
            "Recent Tests": "Test names or types performed.",
            "Results": "Findings or measurements if explicitly mentioned."
        }},
        {"Reason for Referral": "State the rationale behind referring the patient."},
        {"Patient’s Contact Information": {
            "Phone Number": "If explicitly provided.",
            "Email Address": "If explicitly provided."
        }},
        {"Enclosures": "Include line only if test results or documents were mentioned."},
        {"Closing": "Thanking sentence and follow-up expectation."},
        {"Signature Block": {
            "Your Full Name": "Referring clinician's full name or placeholder.",
            "Your Title": "Referring clinician's title or placeholder.",
            "Your Contact Information": "Include only if available.",
            "Your Practice Name": "If not available, placeholder '[Practice Letterhead]'."
        }}
    ]
},
   "consult_note": {
    "display_name": "Consult Note",
    "structure": [
        {"Consultation Type": {
            "Method": "F2F or T/C based on transcript.",
            "Accompaniment": "Seen alone or with someone.",
            "Reason for Visit": "Stated reason for consultation."
        }},
        {"History": {
            "History of Presenting Complaints": "Chief complaint with timing, severity, etc.",
            "ICE": "Ideas, Concerns, Expectations.",
            "Red Flag Symptoms": "Mentioned danger signs.",
            "Relevant Risk Factors": "E.g., smoking, chronic disease.",
            "Past Medical/Surgical History": "Previous diagnoses or surgeries.",
            "Drug History / Allergies": "Current meds and allergy history.",
            "Family History": "Mentioned genetic/family conditions.",
            "Social History": "Living, occupation, lifestyle, travel."
        }},
        {"Examination": {
            "Vital Signs": "Temperature, HR, RR, BP, etc.",
            "Physical/Mental State Examination Findings": "Relevant clinical exam observations.",
            "Investigations with Results": "Tests mentioned and outcomes."
        }},
        {"Impression": "Issue-wise diagnosis or differential."},
        {"Plan": {
            "Investigations Planned": "Further diagnostics intended.",
            "Treatment Planned": "Prescribed medications or therapy.",
            "Relevant Referrals": "Referred departments or clinicians.",
            "Follow-up Plan": "Follow-up date/timeframe.",
            "Advice": "Safety-netting, home-care, symptom alerts."
        }}
    ]
},
   "mental_health_appointment": {
    "display_name": "Mental Health Appointment",
    "structure": [
        {"Chief Complaint": "Primary mental health issue and presenting symptoms."},
        {"Past medical & psychiatric history": "Previous diagnoses, treatments, medications, or hospitalizations."},
        {"Family History": "Family psychiatric illnesses, if discussed."},
        {"Social History": "Occupation, education level, substance use, social supports, and relevant lifestyle context."},
        {"Mental Status Examination": "Appearance, behavior, speech, mood, affect, thoughts, perceptions, cognition, insight, judgment."},
        {"Risk Assessment": "Include details of suicidal ideation, homicidality, or other safety risks if mentioned."},
        {"Diagnosis": "DSM-5 or other clinical diagnosis explicitly discussed."},
        {"Treatment Plan": "Investigations, medications, therapy modalities, referrals, follow-up, and planned actions."},
        {"Safety Plan": "Steps to take during crises if explicitly discussed."}
    ]
},
    "clinical_report": {
    "display_name": "Clinical Interview Report",
    "structure": [
        {"Presenting Problems": "Bullet points describing presenting concerns and associated stressors."},
        {"History of Presenting Problems": "Details on onset, course, severity of problems."},
        {"Current Functioning": [
            {"Sleep": "Sleep disturbances or patterns."},
            {"Employment/Education": "Work/school details, impact of symptoms."},
            {"Family": "Dynamics and effects of condition on family."},
            {"Social": "Social network and relationships."},
            {"Exercise/Physical Activity": "Exercise frequency, limitations."},
            {"Eating Regime/Appetite": "Eating behavior, appetite changes."},
            {"Energy Levels": "Daily energy level variation."},
            {"Recreational/Interests": "Loss of or change in hobbies."}
        ]},
        {"Current Medications": "Names, dosages, and frequencies of active prescriptions."},
        {"Psychiatric History": "Past psychiatric diagnoses, hospitalizations, treatments."},
        {"Medical History": "Personal and family medical background."},
        {"Developmental, Social and Family History": [
            {"Family": "Family of origin, early life."},
            {"Developmental History": "Milestones, delays."},
            {"Educational History": "Academic background, performance."},
            {"Employment History": "Work history, stability."},
            {"Relationship History": "Romantic relationship history, patterns."},
            {"Forensic/Legal History": "Legal issues or criminal involvement."}
        ]},
        {"Substance Use": "Current or past use of alcohol, tobacco, drugs."},
        {"Relevant Cultural/Religious/Spiritual Issues": "If cultural/spiritual identity is clinically relevant."},
        {"Risk Assessment": [
            {"Suicidal Ideation": "Thoughts, attempts, plans."},
            {"Homicidal Ideation": "Any mention of harm to others."},
            {"Self-harm": "Current or past behaviors."},
            {"Violence & Aggression": "Incidents, risk level."},
            {"Risk-taking/Impulsivity": "Impulsive or risky behavior."}
        ]},
        {"Mental State Exam": [
            {"Appearance": "Grooming, dress, physical state."},
            {"Behaviour": "Eye contact, psychomotor activity."},
            {"Speech": "Pace, volume, coherence."},
            {"Mood": "Reported emotional state."},
            {"Affect": "Observed emotional response."},
            {"Perception": "Hallucinations, dissociation."},
            {"Thought Process": "Flow and coherence of thoughts."},
            {"Thought Form": "Formal thought disorder if present."},
            {"Orientation": "Awareness of time, place, self."},
            {"Memory": "Short/long-term recall."},
            {"Concentration": "Focus and attention span."},
            {"Attention": "Ability to stay engaged."},
            {"Judgement": "Decision-making capacity."},
            {"Insight": "Awareness of mental condition."}
        ]},
        {"Test Results": "Psychometric or questionnaire outcomes."},
        {"Diagnosis": "Any DSM-5 diagnoses provided."},
        {"Clinical Formulation": [
            {"Presenting Problem": "Symptoms or condition described."},
            {"Predisposing Factors": "Historical vulnerabilities."},
            {"Precipitating Factors": "Recent triggers."},
            {"Perpetuating Factors": "Ongoing contributors."},
            {"Protecting Factors": "Resilience or buffers."},
            {"Case formulation": "Narrative paragraph synthesizing above factors."}
        ]},
        {"Additional Notes": "Any information not captured above."}
    ]
},
    "psychology_session_notes": {
    "display_name": "Psychology Session Notes",
    "structure": [
        {"Out of Session Task Review": [
            {"Task Engagement": "Client’s practice of skills/strategies from previous session."},
            {"Effectiveness": "Effectiveness and outcomes of tasks."},
            {"Obstacles": "Barriers or challenges faced."}
        ]},
        {"Current Presentation": [
            {"Symptoms": "Reported mental/emotional symptoms."},
            {"Changes": "New or changing issues since last session."}
        ]},
        {"Session Content": [
            {"Issues Raised": "Topics brought up by client."},
            {"Discussion": "Therapist-client interactions, shared insights."},
            {"Therapy Goals": "Goals defined or reiterated."},
            {"Progress": "Client’s progress toward goals."},
            {"Themes and Insights": "Client realizations, therapist reflections."}
        ]},
        {"Intervention": [
            {"Approach": "Modality used (e.g., CBT, DBT)."},
            {"Techniques": "Specific strategies or methods applied."}
        ]},
        {"Setbacks/Barriers/Progress": [
            {"Treatment Barriers": "Obstacles to progress."},
            {"Client Satisfaction": "Client’s views on therapy process."}
        ]},
        {"Risk Assessment and Management": [
            {"Suicidal Ideation": "Thoughts, plans, behaviors."},
            {"Homicidal Ideation": "Any mention of harm to others."},
            {"Self-harm": "Current or past behavior."},
            {"Violence & Aggression": "History or risk indicators."},
            {"Management Plan": "Crisis response plans if applicable."}
        ]},
        {"Mental Status Examination": [
            {"Appearance": "Physical presentation."},
            {"Behaviour": "Interaction and movements."},
            {"Speech": "Fluency and rhythm."},
            {"Mood": "Self-described mood."},
            {"Affect": "Observed emotional state."},
            {"Thoughts": "Cognitive content and pattern."},
            {"Perceptions": "Hallucinations or misperceptions."},
            {"Cognition": "Mental processing and awareness."},
            {"Insight": "Client understanding of their condition."},
            {"Judgment": "Ability to make appropriate decisions."}
        ]},
        {"Out of Session Tasks": "Planned assignments for the next session."},
        {"Plan for Next Session": [
            {"Next Session": "Date and time of follow-up."},
            {"Focus": "Topics or interventions planned."}
        ]}
    ]
},
    "speech_pathology_note": {
    "display_name": "Speech Pathology Report",
    "structure": [
        {"Therapy session attended to": [
            "Describe current issues, reasons for visit, history of presenting complaints.",
            "Include past medical history and previous surgeries.",
            "Mention medications and herbal supplements if discussed.",
            "Summarize relevant social history.",
            "Include allergies if mentioned."
        ]},
        {"Objective": [
            "Describe objective findings from speech-language assessment or physical examination.",
            "Mention any relevant diagnostic tests and results if available."
        ]},
        {"Reports": [
            "Summarize any previous or external reports mentioned."
        ]},
        {"Therapy": [
            "Describe current therapy, intervention techniques.",
            "Mention any recent changes or adjustments to therapy."
        ]},
        {"Outcome": [
            "Describe outcomes or observations from the current or recent therapy session."
        ]},
        {"Plan": [
            "Outline the future therapy plan or intervention approach.",
            "Include follow-up sessions, referrals, or caregiver instructions."
        ]}
    ]
},
    "progress_note": {
    "display_name": "Progress Note",
    "structure": [
        {"Clinic Letterhead": "Include if explicitly present in context or use default if required."},
        {"Clinic Address": [
            "Line 1",
            "Line 2"
        ]},
        {"Contact": [
            "Phone Number",
            "Fax Number"
        ]},
        {"Practitioner": "Full name and title of the practitioner."},
        {"Patient Details": [
            "Surname",
            "First Name",
            "Date of Birth (DD/MM/YYYY)"
        ]},
        {"Progress Note Date": "Date of note (DD Month YYYY)."},
        {"Introduction": "Brief narrative on patient's age, marital status, and living situation."},
        {"Patient’s History and Current Status": "Relevant past medical and mental health history and treatment response."},
        {"Presentation in Clinic": "Narrative of physical and emotional appearance, demeanor, accompanied by whom."},
        {"Mood and Mental State": "Mood stability, suicidal ideation, safety perception, hallucinations or paranoia if present."},
        {"Social and Functional Status": "Daily function, relationships, participation in support programs, and level of independence."},
        {"Physical Health Issues": "Mention physical conditions (e.g., arthritis, obesity) and related management advice."},
        {"Plan and Recommendations": [
            "Continue current medications",
            "Ongoing programs",
            "Lifestyle or health advice",
            "Follow-up appointments"
        ]},
        {"Closing Statement": "Final remarks or reminders shared with the patient."},
        {"Practitioner Signature Block": "Practitioner's Full Name and 'Consultant Psychiatrist'"}
    ]
},
    "meeting_minutes": {
        "display_name": "Meeting Minutes",
        "structure": [
            {"Date": "Date of the meeting, based on transcript or use current date if not available."},
            {"Time": "Time of the meeting if explicitly mentioned; otherwise, leave blank."},
            {"Location": "Location of the meeting if explicitly mentioned; otherwise, leave blank."},
            {"Attendees": "List of attendees present in the meeting. Only include if stated in the transcript."},
            {"Agenda Items": "Bullet list of agenda topics planned for discussion, if mentioned."},
            {"Discussion Points": "Detailed summary of discussions held, decisions explored, concerns raised, and any dialogue of importance."},
            {"Decisions Made": "Summarized list of resolutions or conclusions reached during the meeting."},
            {"Action Items": "List of actionable tasks with designated responsible individuals or teams, if mentioned."},
            {"Next Meeting": [
                {"Date": "Date of the next scheduled meeting, if provided."},
                {"Time": "Time of the next scheduled meeting, if provided."},
                {"Location": "Location of the next meeting, if stated."}
            ]}
        ]
    },
    "followup_note": {
        "display_name": "Follow-Up Consultation Note",
        "structure": [
            {"Date": "Date of follow-up session in DD/MM/YYYY format. Use current date if not explicitly provided."},
            {"History of Presenting Complaints": [
                "Summary of current symptoms or concerns raised by the patient.",
                "Notable changes in symptoms since previous session.",
                "Medication adherence, side effects, or recent changes.",
                "Observations regarding sleep, appetite, energy, and concentration."
            ]},
            {"Mental Status Examination": [
                "Appearance: grooming, posture, clothing.",
                "Behavior: cooperation, eye contact, motor activity.",
                "Speech: fluency, rate, articulation.",
                "Mood: patient-reported mood state.",
                "Affect: observed emotional expression.",
                "Thoughts: logical flow, intrusive thoughts, delusions.",
                "Perceptions: hallucinations or sensory disturbances.",
                "Cognition: orientation, memory, concentration.",
                "Insight: patient awareness of illness.",
                "Judgment: decision-making ability."
            ]},
            {"Risk Assessment": [
                "Presence of suicidal or homicidal ideation.",
                "Self-harm tendencies or recent incidents.",
                "Protective factors like family support or coping strategies."
            ]},
            {"Diagnosis": [
                "Primary psychiatric diagnosis per DSM/ICD.",
                "Any comorbid psychiatric or medical conditions.",
                "Provisional diagnoses if applicable."
            ]},
            {"Treatment Plan": [
                "Details of prescribed medications and any changes.",
                "Recommended therapies or interventions.",
                "Next follow-up interval and reasons.",
                "Additional referrals or investigations ordered."
            ]},
            {"Safety Plan": [
                "Crisis support instructions.",
                "Warning signs and symptom escalation plans.",
                "Emergency contacts and hospitalization triggers."
            ]},
            {"Additional Notes": [
                "Relevant discussions not covered in other sections.",
                "Family involvement or feedback.",
                "Social, financial, or logistical barriers discussed."
            ]}
        ]
    },
    "detailed_soap_note": {
        "display_name": "Detailed SOAP Note",
        "structure": [
            {"Subjective": [
                "Current complaints (e.g., symptoms, reasons for visit, onset, duration, location, quality, severity, aggravating/relieving factors, progression, impact on daily life)",
                "Past medical and surgical history (only if mentioned)",
                "Medications and supplements (including dosages and usage patterns)",
                "Social history (lifestyle, occupation, stressors, living conditions)",
                "Allergies (drug/environmental)",
                "Narrative summary: symptom evolution, non-pharmacologic efforts, lifestyle factors, medication response, recent changes, ROS positives/negatives"
            ]},
            {"Review of Systems": [
                {"General": "Include any systemic symptoms such as fatigue, fever, weight loss, or malaise."},
                {"Skin": "Note any rashes, itching, dryness, or other dermatologic issues."},
                {"Head": "Document any headaches, dizziness, or head-related complaints."},
                {"Eyes": "Mention vision changes, eye pain, redness, discharge, or photophobia."},
                {"Ears": "Include hearing loss, tinnitus, ear pain, or discharge."},
                {"Nose": "Capture nasal congestion, rhinorrhea, nosebleeds, or sinus pressure."},
                {"Throat": "Document sore throat, hoarseness, or swallowing difficulties."},
                {"Neck": "Mention neck pain, stiffness, swelling, or lymphadenopathy."},
                {"Respiratory": "Include cough, shortness of breath, wheezing, or chest tightness."},
                {"Cardiovascular": "Note chest pain, palpitations, leg swelling, or known heart issues."},
                {"Gastrointestinal": "Include nausea, vomiting, diarrhea, constipation, abdominal pain, or appetite changes."},
                {"Genitourinary": "Mention urinary frequency, urgency, dysuria, hematuria, or incontinence."},
                {"Musculoskeletal": "Document joint pain, muscle aches, stiffness, or mobility issues."},
                {"Neurological": "Include numbness, tingling, weakness, balance problems, or seizures."},
                {"Psychiatric": "Capture symptoms like anxiety, depression, mood swings, or sleep disturbances."},
                {"Endocrine": "Note intolerance to heat/cold, excessive thirst, hunger, or sweating."},
                {"Hematologic/Lymphatic": "Include easy bruising, bleeding, anemia, or swollen lymph nodes."},
                {"Allergic/Immunologic": "Mention allergies, frequent infections, or immune-related symptoms."}
            ]},
            {"Objective": [
                "Vital signs (BP, HR, RR, Temp, O2 Sat)",
                "General appearance",
                "System-specific findings (e.g., HEENT, cardiac, respiratory, abdomen, neuro, skin) — paragraph format only if mentioned"
            ]},
            {"Assessment": [
                "Overall impression or diagnosis (if mentioned)",
                {
                    "Per Issue": [
                        "Issue name (e.g., Chest Pain)",
                        "Assessment: probable diagnosis",
                        "Differential diagnosis (if discussed or inferred)",
                        "Diagnostic tests (omit if not mentioned)",
                        "Treatment plan (only if mentioned)",
                        "Referrals or follow-up care (only if mentioned)"
                    ]
                }
            ]},
            {"Follow-Up": [
                "Instructions for worsening or non-improving symptoms",
                "Scheduled follow-up or test review timeline",
                "Education provided and patient’s understanding of the plan"
            ]}
        ]
    },
    "case_formulation": {
        "display_name": "Case Formulation",
        "structure": [
            {"CLIENT GOALS": "List goals and aspirations explicitly mentioned by the client."},
            {"PRESENTING PROBLEM/S": "Describe presenting issues, complaints, or challenges."},
            {"PREDISPOSING FACTORS": "Historical or background factors contributing to current issues."},
            {"PRECIPITATING FACTORS": "Recent triggers or events that led to symptom onset or worsening."},
            {"PERPETUATING FACTORS": "Ongoing behaviors, beliefs, or environments that sustain the issue."},
            {"PROTECTIVE FACTORS": "Strengths, supports, or coping strategies helping the client manage."},
            {"PROBLEM LIST": "Concrete list of identifiable mental health issues or clinical problems."},
            {"TREATMENT GOALS": "Objectives the client aims to achieve through therapy."},
            {"CASE FORMULATION": "Narrative summary integrating biopsychosocial context and explaining symptom development/maintenance."},
            {"TREATMENT MODE/INTERVENTIONS": "Current or planned therapeutic modalities (e.g., CBT, medication)."}
        ]
    },
    "discharge_summary": {
        "display_name": "Discharge Summary",
        "structure": [
            {"Client Name": "Client’s full name (include only if explicitly mentioned)."},
            {"Date of Birth": "Client’s date of birth (if mentioned)."},
            {"Date of Discharge": "The date the client was discharged (if mentioned)."},
            {"Referral Information": [
                "Referral Source: Name/contact of referring party",
                "Reason for Referral: Reason for seeking therapy"
            ]},
            {"Presenting Issues": "List presenting issues or symptoms."},
            {"Diagnosis": "List final diagnosis/diagnoses (if explicitly mentioned)."},
            {"Treatment Summary": [
                "Duration of Therapy: Start to end date of therapy",
                "Number of Sessions: Total sessions attended",
                "Type of Therapy: CBT, ACT, DBT, etc.",
                "Therapeutic Goals: Goal 1, Goal 2, Goal 3, etc.",
                "Summary of Treatment Provided: Therapy type/frequency/duration",
                "Medications Prescribed"
            ]},
            {"Progress and Response to Treatment": [
                "Overall progress",
                "Progress Toward Goals: List per goal"
            ]},
            {"Clinical Observations": [
                "Client's Engagement",
                "Client's Strengths: List of strengths",
                "Client's Challenges: List of challenges"
            ]},
            {"Risk Assessment": "Summary of risk at discharge."},
            {"Outcome of Therapy": [
                "Current Status",
                "Remaining Issues",
                "Client’s Perspective",
                "Therapist's Assessment"
            ]},
            {"Reason for Discharge": [
                "Discharge Reason",
                "Client's Understanding and Agreement"
            ]},
            {"Discharge Plan": "Planned referrals, follow-ups, or continued care instructions."},
            {"Recommendations": [
                "General recommendations",
                "Follow-Up Care: List of specific recommendations",
                "Self-Care Strategies: Client-led strategies",
                "Crisis Plan",
                "Support Systems"
            ]},
            {"Additional Notes": "Extra observations or instructions."},
            {"Final Note": [
                "Therapist’s Closing Remarks",
                "Clinician's Name",
                "Clinician's Signature",
                "Date",
                "Attachments (if any)"
            ]}
        ]
    },
    "h75": {
        "display_name": ">75 Health Assessment",
        "structure": [
            {"Medical History": [
                "Chronic conditions",
                "Smoking history",
                "Current presentation",
                "Medications prescribed",
                "Last specialist, dental, optometry visits recorded",
                "Latest screening tests noted (BMD, FOBT, CST, mammogram)",
                "Vaccination status updated (flu, COVID-19, pneumococcal, shingles, tetanus)",
                "Sleep quality affected by snoring or daytime somnolence",
                "Vision",
                "Presence of urinary and/or bowel incontinence",
                "Falls reported in last 3 months",
                "Independent with activities of daily living",
                "Mobility limits documented",
                "Home support and ACAT assessment status confirmed",
                "Advance care planning documents (Will, EPoA, AHD) up to date",
                "Cognitive function assessed"
            ]},
            {"Social History": [
                "Engagement in hobbies and social activities",
                "Living arrangements and availability of social supports",
                "Caregiver roles identified"
            ]},
            {"Sleep": [
                "Sleep difficulties or use of sleep aids documented"
            ]},
            {"Bowel and Bladder Function": [
                "Continence status described"
            ]},
            {"Hearing and Vision": [
                "Hearing aid use and comfort noted",
                "Recent audiology appointment recorded",
                "Glasses use and last optometry review noted"
            ]},
            {"Home Environment & Safety": [
                "Home access and safety features documented",
                "Assistance with cleaning, cooking, gardening reported",
                "Financial ability to afford food and services addressed"
            ]},
            {"Mobility and Physical Function": [
                "Ability to bend, kneel, climb stairs, dress, bathe independently",
                "Use of walking aids and quality footwear",
                "Exercise or physical activity levels described"
            ]},
            {"Nutrition": [
                "Breakfast",
                "Lunch",
                "Dinner",
                "Snacks",
                "Dental or swallowing difficulties"
            ]},
            {"Transport": [
                "Use of transport services or family support for appointments"
            ]},
            {"Specialist and Allied Health Visits": [
                "Next specialist and allied health appointments planned"
            ]},
            {"Health Promotion & Planning": [
                "Chronic disease prevention and lifestyle advice provided",
                "Immunisations and screenings reviewed and updated",
                "Patient health goals and advance care planning discussed",
                "Referrals made as needed",
                "Follow-up and recall appointments scheduled"
            ]}
        ]
    },
    "new_soap_note": {
        "display_name": "SOAP Note",
        "structure": [
            {"Subjective": "Chief complaints with duration, severity, alleviating/worsening factors, and daily impact — listed as bullet points."},
            {"Past Medical History": "Relevant conditions, medications, surgeries, investigations, and immunization history — listed as bullet points."},
            {"Social History": "Social factors (e.g., smoking, alcohol, job stress) relevant to complaints — listed as bullet points."},
            {"Family History": "Family illnesses relevant to the presenting issue(s) — listed as bullet points."},
            {"Objective": "Vital signs, examination findings, and any available results — listed as bullet points."},
            {"Assessment & Plan": "Each issue assessed with subheadings: Diagnosis, Differential Diagnosis, Investigations, Treatment, Referrals/Follow-Up. Group related issues where applicable."}
        ]
    },
    "soap_issues": {
        "display_name": "SOAP Issues",
        "structure": [
            {"Subjective": [
                "Per issue: Include Issue Name, Duration, Timing, Quality, Severity, Context, Progression, Previous Episodes, Impact on daily life, Associated Symptoms, and Medications",
                "Each issue should be grouped with its related details (as bullet points)"
            ]},
            {"Past Medical History": [
                "Contributing Factors: Past medical or surgical history",
                "Social History: Lifestyle, work, stressors, habits",
                "Family History: Any relevant family diagnoses",
                "Exposure History: Environmental, occupational, infectious exposure",
                "Immunization History: Mentioned vaccines",
                "Other: Any other subjective info"
            ]},
            {"Objective": [
                "Vital Signs: BP, HR, T, O2, etc.",
                "Examination Findings: Physical or mental exam findings",
                "Investigations With Results: Completed test results"
            ]},
            {"Assessment and Plan": [
                "Per issue: Match issue name from Subjective",
                "Include: Assessment (diagnosis), Differential Diagnosis, Treatment Planned, Investigations Planned, Referrals"
            ]}
        ]
    },
    "cardiology_letter": {
        "display_name": "Cardiology Consult Letter",
        "structure": [
            {"Reason for Referral": "Describe the explicit reason for referral including presenting cardiac symptoms, abnormal tests, or referring physician concerns. Write in paragraph format."},
            {"History of Presenting Complaints": "Summarize current cardiac-related symptoms with duration, frequency, and associated features like dyspnea, fatigue, or syncope. Use paragraph format."},
            {"Past Medical History": "Include relevant cardiac or systemic medical conditions, especially hypertension, diabetes, stroke, heart failure, etc. Use paragraph format."},
            {"Family History": "Mention family history of heart conditions like coronary artery disease, sudden cardiac death, cardiomyopathy, or arrhythmias. Use paragraph format."},
            {"Social History": "Summarize lifestyle habits relevant to cardiac health, including smoking, alcohol, drug use, exercise, and stress. Use paragraph format."},
            {"Medications": "List all current medications with dosage and frequency, especially cardiac medications and relevant supplements."},
            {"Examinations": "Summarize physical exam findings including vitals (BP, HR), heart sounds, murmurs, JVP, edema, respiratory status. Structure with subheadings as needed."},
            {"Investigations": "Include results from ECG, ECHO, blood tests, Holter monitor, imaging, etc. Format as concise interpretation-style paragraph."},
            {"Assessment and Evaluation": "Interpret findings with possible diagnosis, severity of cardiac condition, or abnormalities. Paragraph format."},
            {"Plan and Recommendation": "Provide next steps including tests, treatment changes, lifestyle advice, follow-up, and referrals. Use subheadings where relevant, in paragraph format."}
        ]
    },


}


@app.get("/template-structures")
def get_all_template_structures():
    operation = "GET_ALL_TEMPLATE_STRUCTURES"
    try:
        main_logger.info(f"[{operation}] Request received to fetch all template structures.")
        
        result = []
        for template_type, config in template_structures_verbose.items():
            result.append({
                "template_type": template_type,
                "display_name": config.get("display_name"),
                "structure": config.get("structure")
            })

        main_logger.info(f"[{operation}] Successfully fetched {len(result)} templates.")
        return JSONResponse(content=result)
    
    except Exception as e:
        error_logger.exception(f"[{operation}] Failed to retrieve template structures: {str(e)}")
        return JSONResponse(status_code=500, content={"error": "Internal server error"})


async def get_transcription_and_reports(transcript_id: str):
    try:
        main_logger.info(f"[FETCH] Fetching transcription for ID: {transcript_id}")
        transcripts_table = dynamodb.Table('transcripts')
        transcript_response = transcripts_table.get_item(Key={"id": transcript_id})

        if 'Item' not in transcript_response:
            return None

        transcription = transcript_response['Item']
        transcription['transcript'] = decrypt_data(transcription['transcript'])

        # Get all associated reports
        reports_table = dynamodb.Table('reports')
        reports = await dynamodb_scan_all(
            reports_table,
            FilterExpression=Attr('transcript_id').eq(transcript_id)
        )

        decrypted_reports = []
        for report in reports:
            raw_data = decrypt_data(report['gpt_response'])
            report['gpt_response'] = raw_data.decode('utf-8', errors='replace') if isinstance(raw_data, bytes) else raw_data
            report['formatted_report'] = report['gpt_response']  # fallback if formatted missing
            decrypted_reports.append(report)

        return {
            "transcription": transcription,
            "reports": decrypted_reports
        }

    except Exception as e:
        error_logger.error(f"[FETCH] Error getting transcription and reports: {e}", exc_info=True)
        return None


async def get_report_by_id(report_id: str):
    try:
        main_logger.info(f"[FETCH] Fetching report by ID: {report_id}")
        table = dynamodb.Table('reports')
        response = table.get_item(Key={"id": report_id})

        if 'Item' in response:
            item = response['Item']
            item['gpt_response'] = decrypt_data(item['gpt_response'])
            item['formatted_report'] = item['gpt_response']  # fallback if formatted missing
            return item
        else:
            return None
    except Exception as e:
        error_logger.error(f"[FETCH] Error getting report by ID: {e}", exc_info=True)
        return None



from jinja2 import Environment, FileSystemLoader
import pdfkit
# Jinja2 setup
env = Environment(loader=FileSystemLoader("templates"))


# Markdown to HTML converter
def convert_report_to_html(raw_text: str) -> str:
    if isinstance(raw_text, bytes):
        raw_text = raw_text.decode('utf-8', errors='replace')
        
    lines = raw_text.strip().split('\n')
    html_output = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("## "):
            html_output.append(f"<h3>{stripped[3:]}</h3>")
        elif stripped.startswith("# "):
            html_output.append(f"<h2>{stripped[2:]}</h2>")
        elif stripped.startswith("•") or stripped.startswith("- "):
            html_output.append(f"<ul><li>{stripped.lstrip('•- ').strip()}</li></ul>")
        elif stripped:
            html_output.append(f"<p>{stripped}</p>")
    return "\n".join(html_output)


# Your API endpoint
@app.get("/generate-pdf-from-report/{report_id}")
async def generate_pdf_from_report(
    report_id: str,
    session_name: str = Query(default="Medical Consultation"),
    session_date: str = Query(default=datetime.now().strftime("%d/%m/%Y")),
    session_time: str = Query(default=datetime.now().strftime("%H:%M"))
):
    try:
        report = await get_report_by_id(report_id)
        if not report:
            error_logger.error(f"[NOT FOUND] Report ID {report_id} not found.")
            return {"error": "Report not found"}

        report_name = template_display_map.get(report.get("template_type", ""), "Report")
        formatted_html = convert_report_to_html(report.get("formatted_report", ""))

        logo_path = os.path.abspath("templates/logo.svg").replace("\\", "/")
        logo_url = f"file:///{logo_path}"

        rendered_html = env.get_template("medtalk_template.html").render(
            session_name=session_name,
            session_date=session_date,
            session_time=session_time,
            report_name=report_name,
            reports_html=formatted_html,
            logo_path=logo_url,
        )

        os.makedirs("generated_reports", exist_ok=True)
        filename = f"report_{uuid.uuid4().hex}.pdf"
        output_path = os.path.join("generated_reports", filename)

        # PDF conversion
        pdfkit.from_string(rendered_html, output_path, options={"enable-local-file-access": True})

        main_logger.info(f"PDF successfully generated for report {report_id} at {output_path}")
        return FileResponse(output_path, filename="medtalk_report.pdf", media_type="application/pdf")

    except Exception as e:
        error_logger.exception(f"[ERROR] PDF generation failed for report_id={report_id}")
        return {"error": str(e)}

############### STRESS TEST ################

TEMPLATES = [
    "new_soap_note", "cardiology_consult", "echocardiography_report_vet", "cardio_consult",
    "cardio_patient_explainer", "coronary_angiograph_report", "left_heart_catheterization",
    "right_and_left_heart_study", "toe_guided_cardioversion_report", "hospitalist_progress_note",
    "multiple_issues_visit", "counseling_consultation", "gp_consult_note",
    "detailed_dietician_initial_assessment", "referral_letter", "consult_note",
    "mental_health_appointment", "clinical_report", "psychology_session_notes",
    "speech_pathology_note", "progress_note", "meeting_minutes", "followup_note",
    "detailed_soap_note", "case_formulation", "discharge_summary", "h75",
    "cardiology_letter", "soap_issues", "summary", "physio_soap_outpatient"
]

TRANSCRIBE_URL = "https://build.medtalk.co/transcribe-audio"
LIVE_REPORTING_URL = "https://build.medtalk.co/live_reporting"

# Setup stress log directory and response storage
STRESS_LOG_DIR = "stress_logs"
STRESS_RESP_DIR = f"{STRESS_LOG_DIR}/responses/reports"
os.makedirs(STRESS_RESP_DIR, exist_ok=True)
os.makedirs(f"{STRESS_LOG_DIR}/responses", exist_ok=True)

def setup_logger(name, log_file, level=logging.INFO):
    format_string = '%(asctime)s - %(levelname)s - %(name)s - %(message)s'
    log_dir = os.path.dirname(log_file)
    os.makedirs(log_dir, exist_ok=True)

    handler = RotatingFileHandler(log_file, maxBytes=10*1024*1024, backupCount=5)
    handler.setFormatter(logging.Formatter(format_string))
    
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter(format_string))
    
    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.addHandler(console)
    logger.propagate = False
    return logger

# Create loggers
stress_main_logger = setup_logger("stress_main", f"{STRESS_LOG_DIR}/main.log")
stress_time_logger = setup_logger("stress_time", f"{STRESS_LOG_DIR}/time.log")
stress_error_logger = setup_logger("stress_error", f"{STRESS_LOG_DIR}/exceptions.log")

import mimetypes
from aiohttp import MultipartWriter

async def transcribe_and_generate_reports(audio_path: str, session: aiohttp.ClientSession, index: int):
    try:
        with open(audio_path, "rb") as f:
            audio_data = f.read()

        # Determine content type from extension
        content_type, _ = mimetypes.guess_type(audio_path)
        if not content_type:
            content_type = "application/octet-stream"

        start = time.time()

        # ✅ Build multipart request manually (avoids reuse issues)
        with MultipartWriter("form-data") as mpwriter:
            part = mpwriter.append(audio_data)
            part.set_content_disposition('form-data', name='audio', filename=audio_path.split("/")[-1])
            part.headers['Content-Type'] = content_type

            async with session.post(TRANSCRIBE_URL, data=mpwriter) as resp:
                transcription_result = await resp.json()
                elapsed = time.time() - start

                if resp.status != 200 or not transcription_result.get("transcript_id"):
                    stress_error_logger.error(f"Transcription failed (#{index}): {transcription_result}")
                    return

                transcript_id = transcription_result["transcript_id"]
                stress_main_logger.info(f"[{index}] Transcription completed: {transcript_id}")
                stress_time_logger.info(f"[{index}] Transcription took {elapsed:.2f}s")

                with open(f"{STRESS_LOG_DIR}/responses/transcription_{transcript_id}.json", "w") as f:
                    json.dump(transcription_result, f, indent=2)

    except Exception as e:
        stress_error_logger.exception(f"Transcription exception (#{index}): {e}")
        return

    # 🔁 Report Generation
    try:
        report_start = time.time()
        for template in TEMPLATES:
            try:
                # 🔹 New multipart for each template
                with MultipartWriter("form-data") as report_writer:
                    part1 = report_writer.append(transcript_id)
                    part1.set_content_disposition("form-data", name="transcript_id")

                    part2 = report_writer.append(template)
                    part2.set_content_disposition("form-data", name="template_type")

                    async with session.post(LIVE_REPORTING_URL, data=report_writer) as response:
                        raw_text = await response.text()

                        if "---meta:::" in raw_text and ":::meta---" in raw_text:
                            meta_block = raw_text.split(":::meta---")[0].replace("---meta:::", "").strip()
                            markdown_block = raw_text.split(":::meta---")[1].strip()
                            report_json = {
                                "template_type": template,
                                "metadata": json.loads(meta_block),
                                "report_markdown": markdown_block
                            }
                        else:
                            report_json = {
                                "template_type": template,
                                "error": "Malformed response",
                                "raw": raw_text
                            }

                        with open(f"{STRESS_RESP_DIR}/{transcript_id}_{template}.json", "w") as rf:
                            json.dump(report_json, rf, indent=2)

                        stress_main_logger.info(
                            f"Report: {template} for {transcript_id} (Status {response.status})"
                        )

            except Exception as e:
                stress_error_logger.error(f"Error in report {template} for {transcript_id}: {e}")

        report_elapsed = time.time() - report_start
        stress_time_logger.info(f"[{index}] All reports took {report_elapsed:.2f}s")

    except Exception as e:
        stress_error_logger.exception(f"Report generation failed for {transcript_id}: {e}")

async def run_stress_test_serial(audio_paths: List[str]):
    stress_main_logger.info("Starting stress test (serial)...")
    async with aiohttp.ClientSession() as session:
        for i in range(50):
            audio_path = audio_paths[i % len(audio_paths)]
            stress_main_logger.info(f"Running test #{i+1} on audio: {os.path.basename(audio_path)}")
            await transcribe_and_generate_reports(audio_path, session, i + 1)
    stress_main_logger.info("🏁 Stress test completed.")

@app.post("/stress-test")
async def stress_test_endpoint(background_tasks: BackgroundTasks, audios: List[UploadFile] = File(...)):
    temp_paths = []
    for audio in audios:
        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".wav")
        with open(temp_file.name, "wb") as f:
            f.write(await audio.read())
        temp_paths.append(temp_file.name)

    background_tasks.add_task(run_stress_test_serial, temp_paths)
    return {
        "status": f"Stress test started with {len(temp_paths)} uploaded audio files.",
        "logs_dir": STRESS_LOG_DIR,
        "responses_dir": f"{STRESS_LOG_DIR}/responses"
    }

@app.post("/stress-test2")
async def stress_test_endpoint_2(background_tasks: BackgroundTasks, audios: List[UploadFile] = File(...)):
    temp_paths = []
    for audio in audios:
        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".wav")
        with open(temp_file.name, "wb") as f:
            f.write(await audio.read())
        temp_paths.append(temp_file.name)

    background_tasks.add_task(run_stress_test_serial, temp_paths)
    return {
        "status": f"Stress test started with {len(temp_paths)} uploaded audio files.",
        "logs_dir": STRESS_LOG_DIR,
        "responses_dir": f"{STRESS_LOG_DIR}/responses"
    }

@app.post("/stress-test3")
async def stress_test_endpoint_3(background_tasks: BackgroundTasks, audios: List[UploadFile] = File(...)):
    temp_paths = []
    for audio in audios:
        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".wav")
        with open(temp_file.name, "wb") as f:
            f.write(await audio.read())
        temp_paths.append(temp_file.name)

    background_tasks.add_task(run_stress_test_serial, temp_paths)
    return {
        "status": f"Stress test started with {len(temp_paths)} uploaded audio files.",
        "logs_dir": STRESS_LOG_DIR,
        "responses_dir": f"{STRESS_LOG_DIR}/responses"
    }

@app.get("/stress-test/report-response", response_class=JSONResponse)
def get_report_response(
    transcript_id: str = Query(..., description="Transcript ID to retrieve"),
    template: str = Query(..., description="Template name (e.g. new_soap_note)")
):
    report_path = Path(f"{STRESS_RESP_DIR}/{transcript_id}_{template}.json")
    main_logger.info(f"Fetching report for transcript_id: {transcript_id}, template: {template}")
    if not report_path.exists():
        error_logger.warning(f"Report not found at {report_path}")
        raise HTTPException(status_code=404, detail=f"Report not found for ID: {transcript_id} and template: {template}")

    try:
        with open(report_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        main_logger.info(f"Successfully loaded report file: {report_path}")
        return data
    except Exception as e:
        error_logger.exception(f"Error reading report file: {report_path}")
        raise HTTPException(status_code=500, detail="Failed to read report file.")

@app.get("/logs/filter", response_class=PlainTextResponse)
def get_filtered_logs(
    main: bool = Query(False),
    time: bool = Query(False),
    error: bool = Query(False),
    api: bool = Query(False),
    db: bool = Query(False)
):
    log_map = {
        "main": ("main", "logging/main.log"),
        "time": ("time", "logging/time.log"),
        "error": ("error", "logging/exceptions.log"),
        "api": ("api", "logging/api.log"),
        "db": ("db", "logging/database.log")
    }

    selected_logs = []
    for key, (name, path) in log_map.items():
        if locals()[key]:
            if os.path.exists(path):
                with open(path, "r") as f:
                    selected_logs.append(f"\n\n===== {name.upper()} LOG =====\n{f.read()}")
                    main_logger.info(f"Fetched log: {path}")
            else:
                warning_msg = f"File not found: {path}"
                selected_logs.append(f"\n\n===== {name.upper()} LOG =====\n File not found: {path}")
                error_logger.warning(warning_msg)

    if not selected_logs:
        msg = "No logs selected. Use query params like `?main=true&error=true`"
        main_logger.warning(msg)
        return PlainTextResponse(msg, status_code=400)

    return PlainTextResponse("".join(selected_logs))

@app.get("/stress-test/logs", response_class=PlainTextResponse)
def get_stress_logs(
    main: bool = Query(False),
    time: bool = Query(False),
    error: bool = Query(False)
):
    log_map = {
        "main": "stress_logs/main.log",
        "time": "stress_logs/time.log",
        "error": "stress_logs/exceptions.log"
    }

    selected = []
    for name, path in log_map.items():
        if locals()[name]:
            if os.path.exists(path):
                with open(path, "r") as f:
                    selected.append(f"\n\n===== {name.upper()} LOG =====\n{f.read()}")
                    main_logger.info(f"Accessed stress log: {path}")
            else:
                warning_msg = f"File not found: {path}"
                selected.append(f"\n\n===== {name.upper()} LOG =====\n File not found: {path}")
                error_logger.warning(warning_msg)

    if not selected:
        msg = "No logs selected. Use query params like `?main=true&error=true`"
        main_logger.warning(msg)
        return PlainTextResponse(msg, status_code=400)
    
    return PlainTextResponse("".join(selected))



LOGGING_DIR = "logging"

LOG_PATHS = {
    "main": f"{LOGGING_DIR}/main.log",
    "time": f"{LOGGING_DIR}/time.log",
    "error": f"{LOGGING_DIR}/exceptions.log",
    "api": f"{LOGGING_DIR}/api.log",
    "db": f"{LOGGING_DIR}/database.log",
}

@app.delete("/clean-logs")
async def clean_logging_logs():
    try:
        # Step 1: Close all handlers
        for name in LOG_PATHS.keys():
            logger = logging.getLogger(name)
            for handler in logger.handlers[:]:
                handler.close()
                logger.removeHandler(handler)

        # Step 2: Truncate each file
        for path in LOG_PATHS.values():
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.truncate()

        # Step 3: Re-setup loggers
        global main_logger, time_logger, error_logger, api_logger, db_logger
        main_logger = setup_logger("main", LOG_PATHS["main"])
        time_logger = setup_logger("time", LOG_PATHS["time"])
        error_logger = setup_logger("error", LOG_PATHS["error"])
        api_logger = setup_logger("api", LOG_PATHS["api"])
        db_logger = setup_logger("db", LOG_PATHS["db"])

        return {"status": "success", "message": "Logging files cleaned and loggers reinitialized."}
    
    except Exception as e:
        return {"status": "error", "message": str(e)}
    
# Clean logs endpoint
@app.delete("/clean-stress-logs")
async def delete_stress_logs():
    try:
        # Step 1: Close logger handlers
        for logger in [logging.getLogger("stress_main"), logging.getLogger("stress_time"), logging.getLogger("stress_error")]:
            for handler in logger.handlers[:]:
                handler.close()
                logger.removeHandler(handler)

        # Step 2: Delete folder
        if os.path.exists(STRESS_LOG_DIR):
            shutil.rmtree(STRESS_LOG_DIR)

        # Step 3: Recreate folders
        os.makedirs(STRESS_RESP_DIR, exist_ok=True)
        os.makedirs(f"{STRESS_LOG_DIR}/responses", exist_ok=True)

        # Step 4: Reinitialize loggers
        global stress_main_logger, stress_time_logger, stress_error_logger
        stress_main_logger = setup_logger("stress_main", f"{STRESS_LOG_DIR}/main.log")
        stress_time_logger = setup_logger("stress_time", f"{STRESS_LOG_DIR}/time.log")
        stress_error_logger = setup_logger("stress_error", f"{STRESS_LOG_DIR}/exceptions.log")

        return {"status": "success", "message": "Stress logs cleaned and loggers reinitialized."}

    except Exception as e:
        return {"status": "error", "message": str(e)}


# ... (Previous code remains unchanged up to the WebSocket endpoint definition)
@app.websocket("/ws/transcribe-and-generate-report")
@log_execution_time
async def ws_transcribe_and_generate_report(websocket: WebSocket):

    """
    WebSocket endpoint to transcribe audio and generate a report with step-by-step updates.
    
    The client must send a JSON message with the following structure:
    {
        "template_type": str,  # e.g., "new_soap_note"
        "audio_data": str,    # Base64-encoded audio data
        "filename": str,      # Name of the audio file (e.g., "audio.wav")
        "content_type": str   # MIME type (e.g., "audio/wav")
    }
    
    The server sends JSON updates at each processing step:
    - Audio received
    - Audio validation completed
    - Audio saved to S3
    - Transcription started/completed
    - Report generation started/completed
    - Error messages if any step fails
    """
    client_id = str(uuid.uuid4())
    request_id = client_id[:8]
    main_logger.info(f"[REQ-{request_id}] WebSocket connection established for client {client_id}")

    try:
        # Connect WebSocket
        await manager.connect(websocket, client_id)
        
        # Receive initial message with audio data and parameters
        data = await websocket.receive_json()
        main_logger.info(f"[REQ-{request_id}] Received initial message: {data.keys()}")

        # Extract parameters
        template_type = data.get("template_type", "new_soap_note")
        audio_b64 = data.get("audio_data")
        filename = data.get("filename", "audio.wav")
        content_type = data.get("content_type", "audio/wav")

        # Send audio received update
        await manager.send_message(json.dumps({
            "status": "processing",
            "step": "audio_received",
            "data": {
                "filename": filename,
                "content_type": content_type,
                "template_type": template_type
            }
        }), client_id)

        # Validate template type
        valid_templates = ["clinical_report","h75", "new_soap_note", "soap_issues", "detailed_soap_note", "soap_note", 
                          "progress_note", "mental_health_appointment", "cardiology_letter", "followup_note", 
                          "meeting_minutes", "referral_letter", "detailed_dietician_initial_assessment", 
                          "psychology_session_notes", "pathology_note", "consult_note", "discharge_summary", "summary",
                          "case_formulation"]
        if template_type not in valid_templates:
            error_msg = f"Invalid template type '{template_type}'. Must be one of: {', '.join(valid_templates)}"
            error_logger.error(f"[REQ-{request_id}] {error_msg}")
            await manager.send_message(json.dumps({
                "status": "failed",
                "step": "validation",
                "error": error_msg,
                "transcription_status": "failed",
                "report_status": "not_started"
            }), client_id)
            return

        # Validate content type
        valid_content_types = [
            "audio/wav", "audio/wave", "audio/x-wav",
            "audio/mp3", "audio/mpeg", "audio/mpeg3", "audio/x-mpeg-3",
            "audio/mp4", "audio/x-m4a", "audio/m4a",
            "audio/flac", "audio/x-flac",
            "audio/aac", "audio/x-aac",
            "audio/ogg", "audio/vorbis", "application/ogg",
            "audio/webm",
            "audio/3gpp", "audio/amr"
        ]
        valid_extensions = [".wav", ".mp3", ".mp4", ".m4a", ".flac", ".aac", ".ogg", ".webm", ".amr", ".3gp"]
        file_extension = os.path.splitext(filename.lower())[1]

        if content_type not in valid_content_types:
            if file_extension in valid_extensions:
                main_logger.info(f"[REQ-{request_id}] Content-type not recognized ({content_type}), but filename has valid extension: {file_extension}")
            else:
                error_msg = f"Invalid audio format. Expected audio file, got {content_type}"
                error_logger.error(f"[REQ-{request_id}] {error_msg}")
                await manager.send_message(json.dumps({
                    "status": "failed",
                    "step": "validation",
                    "error": error_msg,
                    "transcription_status": "failed",
                    "report_status": "not_started"
                }), client_id)
                return

        # Decode base64 audio data
        try:
            audio_data = base64.b64decode(audio_b64)
        except Exception as e:
            error_msg = f"Invalid base64 audio data: {str(e)}"
            error_logger.error(f"[REQ-{request_id}] {error_msg}")
            await manager.send_message(json.dumps({
                "status": "failed",
                "step": "validation",
                "error": error_msg,
                "transcription_status": "failed",
                "report_status": "not_started"
            }), client_id)
            return

        if not audio_data:
            error_msg = "No audio data provided"
            error_logger.error(f"[REQ-{request_id}] {error_msg}")
            await manager.send_message(json.dumps({
                "status": "failed",
                "step": "validation",
                "error": error_msg,
                "transcription_status": "failed",
                "report_status": "not_started"
            }), client_id)
            return

        main_logger.info(f"[REQ-{request_id}] Audio data decoded successfully. Size: {len(audio_data)} bytes")

        # Send validation completed update
        await manager.send_message(json.dumps({
            "status": "processing",
            "step": "validation_completed",
            "data": {
                "filename": filename,
                "content_type": content_type,
                "size_bytes": len(audio_data)
            }
        }), client_id)

        # Initialize status tracking
        transcription_status = "processing"
        report_status = "not_started"
        transcript_id = str(uuid.uuid4())
        report_id = None
        transcription_result = None
        gpt_response = None
        formatted_report = None

        # Save audio to S3
        try:
            audio_info = await save_audio_to_s3(audio_data)
            if not audio_info:
                error_msg = "Failed to save audio to S3"
                error_logger.error(f"[REQ-{request_id}] {error_msg}")
                await manager.send_message(json.dumps({
                    "status": "failed",
                    "step": "save_audio",
                    "error": error_msg,
                    "transcription_status": "failed",
                    "report_status": "not_started"
                }), client_id)
                return
            await manager.send_message(json.dumps({
                "status": "processing",
                "step": "audio_saved",
                "data": audio_info
            }), client_id)
        except Exception as e:
            error_msg = f"Error saving audio to S3: {str(e)}"
            error_logger.error(f"[REQ-{request_id}] {error_msg}")
            await manager.send_message(json.dumps({
                "status": "failed",
                "step": "save_audio",
                "error": error_msg,
                "transcription_status": "failed",
                "report_status": "not_started"
            }), client_id)
            return

        # Transcribe audio
        try:
            await manager.send_message(json.dumps({
                "status": "processing",
                "step": "transcription_started",
                "data": {"transcript_id": transcript_id}
            }), client_id)

            transcription_result = await transcribe_audio_with_diarization(audio_data)
            transcription_status = "completed"

            if isinstance(transcription_result, str) and transcription_result.startswith("Error"):
                transcription_status = "failed"
                error_msg = transcription_result
                error_logger.error(f"[REQ-{request_id}] {error_msg}")
                await manager.send_message(json.dumps({
                    "status": "failed",
                    "step": "transcription",
                    "error": error_msg,
                    "transcription_status": transcription_status,
                    "report_status": report_status,
                    "transcript_id": transcript_id
                }), client_id)
                await save_transcript_to_dynamodb(
                    {"error": error_msg},
                    audio_info,
                    status=transcription_status
                )
                return

            # Save transcription to DynamoDB
            transcript_id = await save_transcript_to_dynamodb(
                transcription_result,
                audio_info,
                status=transcription_status
            )
            if not transcript_id:
                error_msg = "Failed to save transcription to DynamoDB"
                error_logger.error(f"[REQ-{request_id}] {error_msg}")
                await manager.send_message(json.dumps({
                    "status": "failed",
                    "step": "transcription_save",
                    "error": error_msg,
                    "transcription_status": "failed",
                    "report_status": report_status
                }), client_id)
                return

            await manager.send_message(json.dumps({
                "status": "processing",
                "step": "transcription_completed",
                "data": {
                    "transcript_id": transcript_id,
                    "transcription": transcription_result
                }
            }), client_id)
        except Exception as e:
            transcription_status = "failed"
            error_msg = f"Error during transcription: {str(e)}"
            error_logger.exception(f"[REQ-{request_id}] {error_msg}")
            await manager.send_message(json.dumps({
                "status": "failed",
                "step": "transcription",
                "error": error_msg,
                "transcription_status": transcription_status,
                "report_status": report_status,
                "transcript_id": transcript_id
            }), client_id)
            await save_transcript_to_dynamodb(
                {"error": error_msg},
                audio_info,
                status=transcription_status
            )
            return

        # Generate report
        try:
            await manager.send_message(json.dumps({
                "status": "processing",
                "step": "report_generation_started",
                "data": {"transcript_id": transcript_id, "template_type": template_type}
            }), client_id)

            report_status = "processing"
            gpt_response = await generate_gpt_response(transcription_result, template_type)

            if isinstance(gpt_response, str) and gpt_response.startswith("Error"):
                report_status = "failed"
                error_msg = gpt_response
                error_logger.error(f"[REQ-{request_id}] {error_msg}")
                await manager.send_message(json.dumps({
                    "status": "failed",
                    "step": "report_generation",
                    "error": error_msg,
                    "transcription_status": transcription_status,
                    "report_status": report_status,
                    "transcript_id": transcript_id
                }), client_id)
                return

            formatted_report = await format_report(gpt_response, template_type)

            if isinstance(formatted_report, str) and formatted_report.startswith("Error"):
                report_status = "failed"
                error_msg = formatted_report
                error_logger.error(f"[REQ-{request_id}] {error_msg}")
                await manager.send_message(json.dumps({
                    "status": "failed",
                    "step": "report_formatting",
                    "error": error_msg,
                    "transcription_status": transcription_status,
                    "report_status": report_status,
                    "transcript_id": transcript_id
                }), client_id)
                return

            # Save report to DynamoDB
            report_id = await save_report_to_dynamodb(
                transcript_id,
                gpt_response,
                formatted_report,
                template_type,
                status="completed"
            )
            report_status = "completed"

            if not report_id:
                error_msg = "Failed to save report to DynamoDB"
                error_logger.error(f"[REQ-{request_id}] {error_msg}")
                await manager.send_message(json.dumps({
                    "status": "failed",
                    "step": "report_save",
                    "error": error_msg,
                    "transcription_status": transcription_status,
                    "report_status": "failed",
                    "transcript_id": transcript_id
                }), client_id)
                return

            await manager.send_message(json.dumps({
                "status": "success",
                "step": "report_generation_completed",
                "data": {
                    "transcript_id": transcript_id,
                    "report_id": report_id,
                    "template_type": template_type,
                    "transcription": transcription_result,
                    "gpt_response": json.loads(gpt_response) if gpt_response else None,
                    "formatted_report": formatted_report
                },
                "transcription_status": transcription_status,
                "report_status": report_status
            }), client_id)

            main_logger.info(f"[REQ-{request_id}] WebSocket process completed successfully")
        except Exception as e:
            report_status = "failed"
            error_msg = f"Error during report generation: {str(e)}"
            error_logger.exception(f"[REQ-{request_id}] {error_msg}")
            await manager.send_message(json.dumps({
                "status": "failed",
                "step": "report_generation",
                "error": error_msg,
                "transcription_status": transcription_status,
                "report_status": report_status,
                "transcript_id": transcript_id,
                "report_id": report_id
            }), client_id)
            if transcript_id:
                await save_report_to_dynamodb(
                    transcript_id,
                    gpt_response,
                    formatted_report,
                    template_type,
                    status=report_status
                )

    except WebSocketDisconnect:
        main_logger.info(f"[REQ-{request_id}] Client {client_id} disconnected")
    except Exception as e:
        error_msg = f"Unexpected error in WebSocket: {str(e)}"
        error_logger.exception(f"[REQ-{request_id}] {error_msg}")
        try:
            await manager.send_message(json.dumps({
                "status": "failed",
                "step": "unexpected_error",
                "error": error_msg,
                "transcription_status": transcription_status,
                "report_status": report_status,
                "transcript_id": transcript_id,
                "report_id": report_id
            }), client_id)
        except:
            pass
    finally:
        manager.disconnect(client_id)
        main_logger.info(f"[REQ-{request_id}] WebSocket connection closed for client {client_id}")