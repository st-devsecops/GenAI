# Executed on  mac
Install Uv on mac
create a dir and run below
uv init
uv venv
source .venv/bin/activate
uv pip install pandas speechrecognition google-genai edge-tts pygame
uv pip install pyaudio

export GEMINI_API_KEY="your_actual_free_key_here"
python free_voice_agent.py
