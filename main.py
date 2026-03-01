import logging
import os
import tempfile
from typing import Optional

import requests
import whisper
from dotenv import load_dotenv
from flask import Flask, Response, request
import threading
from twilio.request_validator import RequestValidator
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse
import google.generativeai as genai
from gtts import gTTS

load_dotenv()

TWILIO_ACCOUNT_SID = os.getenv("ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("AUTH_TOKEN", "")
TWILIO_FROM_NUMBER = os.getenv("FROM", "")
WHISPER_MODEL_NAME = os.getenv("MODEL_NAME", "small")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
PORT = int(os.getenv("PORT", "5000"))
DEBUG_MODE = os.getenv("DEBUG", "false").lower() in {"1", "true", "yes"}
ASYNC_REPLY = os.getenv("ASYNC_REPLY", "false").lower() in {"1", "true", "yes"}

logger = logging.getLogger("audio-transcription-bot")
logging.basicConfig(level=logging.DEBUG if DEBUG_MODE else logging.INFO)

if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
    logger.warning("Twilio credentials are not fully configured. Inbound validation and outbound messages may fail.")

if not GEMINI_API_KEY:
    logger.warning("Gemini API key not configured. AI responses will not work.")
else:
    genai.configure(api_key=GEMINI_API_KEY)

twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN) if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN else None
twilio_validator = RequestValidator(TWILIO_AUTH_TOKEN) if TWILIO_AUTH_TOKEN else None

app = Flask(__name__)

logger.info("Loading Whisper model '%s'...", WHISPER_MODEL_NAME)
whisper_model = whisper.load_model(WHISPER_MODEL_NAME)
logger.info("Whisper model loaded")


def fetch_media_to_tempfile(media_url: str) -> str:
    auth = (TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN) if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN else None
    with tempfile.NamedTemporaryFile(delete=False, suffix=".tmp") as tmp_file:
        with requests.get(media_url, stream=True, timeout=60, auth=auth) as r:
            r.raise_for_status()
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:
                    tmp_file.write(chunk)
        return tmp_file.name


def transcribe_audio_file(file_path: str) -> str:
    result = whisper_model.transcribe(file_path)
    return result.get("text", "")


def get_ai_response(user_message: str) -> str:
    """Get AI response - simple greeting for now"""
    return "Hi, hello, how can I help you?"


def text_to_speech(text: str) -> str:
    """Convert text to speech and return file path"""
    try:
        tts = gTTS(text=text, lang='en', slow=False)
        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
        tts.save(temp_file.name)
        return temp_file.name
    except Exception as e:
        logger.exception("Failed to convert text to speech: %s", e)
        return None


def send_outbound_message(to_e164: str, body: str) -> None:
    if not twilio_client or not TWILIO_FROM_NUMBER:
        logger.error("Twilio client or FROM number not configured; cannot send outbound message")
        return
    from_whatsapp = TWILIO_FROM_NUMBER
    if not from_whatsapp.startswith("whatsapp:"):
        from_whatsapp = f"whatsapp:{from_whatsapp}"
    twilio_client.messages.create(body=body, from_=from_whatsapp, to=to_e164)


def send_outbound_audio(to_e164: str, text_message: str) -> None:
    """Send voice message using Twilio's TTS"""
    if not twilio_client or not TWILIO_FROM_NUMBER:
        logger.error("Twilio client or FROM number not configured")
        return
    
    from_whatsapp = TWILIO_FROM_NUMBER
    if not from_whatsapp.startswith("whatsapp:"):
        from_whatsapp = f"whatsapp:{from_whatsapp}"
    
    try:
        # Use Twilio's built-in TTS by creating a TwiML response
        from twilio.twiml.voice_response import VoiceResponse
        
        # Create voice message using gTTS
        tts = gTTS(text=text_message, lang='en', slow=False)
        
        # Save to static folder
        static_folder = os.path.join(os.path.dirname(__file__), "static")
        os.makedirs(static_folder, exist_ok=True)
        
        import time
        filename = f"audio_{int(time.time())}.mp3"
        static_path = os.path.join(static_folder, filename)
        tts.save(static_path)
        
        # Get ngrok URL
        base_url = os.getenv("NGROK_URL", "https://areally-strawlike-shea.ngrok-free.dev")
        media_url = f"{base_url}/static/{filename}"
        
        logger.info(f"Sending audio from: {media_url}")
        
        # Send as media message
        message = twilio_client.messages.create(
            from_=from_whatsapp,
            to=to_e164,
            media_url=[media_url]
        )
        
        logger.info(f"Voice message sent! SID: {message.sid}")
        
    except Exception as e:
        logger.exception(f"Failed to send audio: {e}")
        # Fallback to text
        send_outbound_message(to_e164, text_message)


def is_audio_content_type(content_type: Optional[str]) -> bool:
    if not content_type:
        return False
    return content_type.lower().startswith("audio/")


@app.get("/")
@app.get("/healthz")
def health() -> tuple[str, int]:
    return "ok", 200


@app.route('/static/<path:filename>')
def serve_static(filename):
    """Serve static audio files"""
    static_folder = os.path.join(os.path.dirname(__file__), "static")
    from flask import send_from_directory
    return send_from_directory(static_folder, filename)


@app.post("/whatsapp")
def whatsapp() -> Response:
    # Temporarily disabled for ngrok testing
    # if twilio_validator:
    #     signature = request.headers.get("X-Twilio-Signature", "")
    #     url = request.url
    #     form = {k: v for k, v in request.form.items()}
    #     if not twilio_validator.validate(url, form, signature):
    #         logger.warning("Invalid Twilio signature")
    #         return Response("Invalid signature", status=403)

    from_number = request.form.get("From", "")
    num_media = int(request.form.get("NumMedia", "0") or 0)
    content_type = request.form.get("MediaContentType0") if num_media > 0 else None
    media_url = request.form.get("MediaUrl0") if num_media > 0 else None
    text_body = request.form.get("Body", "").strip()

    messaging_response = MessagingResponse()

    # Handle text messages
    if num_media == 0 and text_body:
        logger.info(f"Received text: {text_body}")
        ai_response = get_ai_response(text_body)
        logger.info(f"AI Response: {ai_response}")
        messaging_response.message(ai_response)
        return Response(str(messaging_response), mimetype="application/xml")

    # Handle voice messages
    if num_media > 0 and is_audio_content_type(content_type):
        try:
            temp_path = fetch_media_to_tempfile(media_url) if media_url else None
            if not temp_path:
                raise RuntimeError("Failed to download media")

            # Send processing message
            messaging_response.message("🎤 Processing your voice message...")
            
            # Process in background
            def process_voice():
                try:
                    # Transcribe the voice message
                    logger.info("Starting transcription...")
                    transcription = transcribe_audio_file(temp_path).strip() or "(no transcription)"
                    logger.info(f"Transcribed: {transcription}")
                    
                    # Get AI response
                    logger.info("Getting AI response...")
                    ai_response = get_ai_response(transcription)
                    logger.info(f"AI Response: {ai_response}")
                    
                    # Send voice response directly
                    logger.info("Sending voice response...")
                    send_outbound_audio(from_number, ai_response)
                    logger.info("Voice response sent!")
                    
                finally:
                    try:
                        os.remove(temp_path)
                    except Exception:
                        pass
            
            # Start background thread
            threading.Thread(target=process_voice, daemon=True).start()
                    
            return Response(str(messaging_response), mimetype="application/xml")
            
        except Exception as exc:
            logger.exception("Failed to process media: %s", exc)
            messaging_response.message("Sorry, there was an error processing your audio.")
            return Response(str(messaging_response), mimetype="application/xml", status=500)

    # Default response
    messaging_response.message("Send me a text or voice message and I'll respond!")
    return Response(str(messaging_response), mimetype="application/xml")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=DEBUG_MODE)
