import os
import json
import argparse
import time
import logging
import subprocess
import signal
import atexit

from langchain_openai import ChatOpenAI

from agentorg.utils.utils import init_logger
from agentorg.orchestrator.orchestrator import AgentOrg
from create import API_PORT
from agentorg.utils.model_config import MODEL

from openai import OpenAI
from pydub import AudioSegment
from pydub.playback import play

import openai
import sounddevice as sd
import numpy as np
from scipy.io.wavfile import write
import wave
from pynput import keyboard

process = None  # Global reference for the FastAPI subprocess

def terminate_subprocess():
    """Terminate the FastAPI subprocess."""
    global process
    if process and process.poll() is None:  # Check if process is running
        logger.info(f"Terminating FastAPI process with PID: {process.pid}")
        process.terminate()  # Send SIGTERM
        process.wait()  # Ensure it stops
        logger.info("FastAPI process terminated.")

# Register cleanup function to run on program exit
atexit.register(terminate_subprocess)

# Handle signals (e.g., Ctrl+C)
signal.signal(signal.SIGINT, lambda signum, frame: exit(0))
signal.signal(signal.SIGTERM, lambda signum, frame: exit(0))


def get_api_bot_response(args, history, user_text, parameters):
    data = {"text": user_text, 'chat_history': history, 'parameters': parameters}
    orchestrator = AgentOrg(config=os.path.join(args.input_dir, "taskgraph.json"))
    result = orchestrator.get_response(data)

    return result['answer'], result['parameters']


def start_apis():
    """Start the FastAPI subprocess and update task graph API URLs."""
    global process
    command = [
        "uvicorn",
        "agentorg.orchestrator.NLU.api:app",  # Replace with proper import path
        "--port", API_PORT,
        "--host", "0.0.0.0",
        "--log-level", "info"
    ]

    # Redirect FastAPI logs to a file
    with open("./logs/api.log", "w") as log_file:
        process = subprocess.Popen(
            command,
            stdout=log_file,  # Redirect stdout to a log file
            stderr=subprocess.STDOUT,  # Redirect stderr to the same file
            start_new_session=True  # Run in a separate process group
        )
    logger.info(f"Started FastAPI process with PID: {process.pid}")

def text2speech(text):
    # Initialize the required variables and create folders
    client = OpenAI()
    save_path = os.path.join("speech_tmp", "speech.mp3")
    os.makedirs("speech_tmp", exist_ok=True)

    # Convert the text into speech
    with client.audio.speech.with_streaming_response.create(
        model="tts-1",
        voice="alloy",
        input=f"""{text}""",
    ) as response:
        response.stream_to_file(save_path)

    # Play the speech
    audio = AudioSegment.from_file(save_path)
    play(audio)

def record_audio_with_toggle():
    """
    Records audio when 'r' is pressed and stops recording when 's' is pressed.
    Saves the audio to 'output.wav'.
    """
    # Parameters for recording
    sample_rate = 44100  # Sampling frequency
    channels = 1  # Number of audio channels
    dtype = 'int16'  # Data type for audio
    filename = os.path.join("speech_tmp", "recorded_speech.wav")  # Output filename
    os.makedirs("speech_tmp", exist_ok=True)

    # Buffer to store audio data
    audio_data = []
    recording = False
    stop_recording = False
    print("Press 'r' to start recording and 's' to stop.")

    # Event handlers
    def on_press(key):
        nonlocal recording, audio_data, stop_recording
        try:
            if key.char == 'r' and not recording:
                print("Recording started. Press 's' to stop.")
                recording = True
                audio_data = []  # Reset audio buffer
            elif key.char == 's' and recording:
                print("Recording stopped.")
                recording = False
                stop_recording = True
        except AttributeError:
            pass  # Ignore special keys

    # Start the listener
    with keyboard.Listener(on_press=on_press) as listener:
        while not stop_recording:  # Exit the loop when 's' is pressed
            if recording:
                # Capture a chunk of audio
                chunk = sd.rec(int(sample_rate * 0.1), samplerate=sample_rate, channels=channels, dtype=dtype)
                sd.wait()
                audio_data.append(chunk)

    # Concatenate all recorded chunks
    audio_data = np.concatenate(audio_data, axis=0)

    # Save the audio data to a WAV file
    with wave.open(filename, 'wb') as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(np.dtype(dtype).itemsize)
        wf.setframerate(sample_rate)
        wf.writeframes(audio_data.tobytes())

    print(f"Audio saved to {filename}.")
    return filename

def speech2text():
    # Record the audio
    audio_file = record_audio_with_toggle()

    # Detect text
    text_detected = False
    while not text_detected:
        try:
            client = OpenAI()
            with open(audio_file, "rb") as audio_file_obj:
                response = client.audio.transcriptions.create(
                    model="whisper-1",  # Using Whisper model
                    file=audio_file_obj
                )
                text = response.text
                print(f"Recognized text: {text}")
                os.remove(audio_file)  # Clean up temporary file

                # Check if the recognized text is adequate
                if text == '':
                    print("Inadequate text...")
                    text_detected = False
                else:
                    text_detected = True
                    return text
        except Exception as e:
            print(f"Error during transcription: {e}")
            os.remove(audio_file)  # Clean up temporary file
            text_detected = False

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--input-dir', type=str, default="./examples/test")
    parser.add_argument('--model', type=str, default=MODEL["model_type_or_path"])
    parser.add_argument('--log-level', type=str, default="WARNING", choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"])
    parser.add_argument('--sound', action='store_true', help="Enable sound (default is disabled)")
    args = parser.parse_args()
    os.environ["DATA_DIR"] = args.input_dir
    MODEL["model_type_or_path"] = args.model
    log_level = getattr(logging, args.log_level.upper(), logging.WARNING)
    logger = init_logger(log_level=log_level, filename=os.path.join(os.path.dirname(__file__), "logs", "agentorg.log"))

    # Set up audio recording
    start_apis()

    history = []
    params = {}
    config = json.load(open(os.path.join(args.input_dir, "taskgraph.json")))
    user_prefix = "USER"
    worker_prefix = "ASSISTANT"
    for node in config['nodes']:
        if node[1].get("type", "") == 'start':
            start_message = node[1]['attribute']["value"]
            break
    history.append({"role": worker_prefix, "content": start_message})
    print(f"Bot: {start_message}")
    if args.sound:
        text2speech(start_message)

    try:
        while True:
            # Record audio and convert to text using Whisper
            if args.sound:
                user_text = speech2text()  # This line will record and convert audio to text using Whisper
            else:
                user_text = input("You: ")

            if user_text.lower() == "quit":
                break
            start_time = time.time()
            output, params = get_api_bot_response(args, history, user_text, params)
            history.append({"role": user_prefix, "content": user_text})
            history.append({"role": worker_prefix, "content": output})
            print(f"getAPIBotResponse Time: {time.time() - start_time}")
            
            # Print the output and play it
            print(f"Bot: {output}")
            if args.sound:
                text2speech(output)
            
    finally:
        terminate_subprocess()  # Ensure the subprocess is terminated
