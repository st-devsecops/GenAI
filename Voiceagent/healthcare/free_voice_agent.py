# This is on local
import os
import re
# Allows multiple tasks (speech generation, microphone listening, API calls) to run without blocking each other.
import asyncio
import sqlite3
from datetime import datetime
import pandas as pd
import speech_recognition as sr
# Converts text into natural-sounding speech using Microsoft's Edge voices. It's free and doesn't require an API key.
import edge_tts
import pygame
from google import genai
# types → Classes such as GenerateContentConfig, Part, Content, Tool, etc.
from google.genai import types
import time

# Setup local and db
# get gemini ai api key https://aistudio.google.com/
# Set it in your terminal: export GEMINI_API_KEY="your-key"
client = genai.Client()

# Initialize pygame audio mixer
pygame.mixer.init()

# 1. Initialize SQLite database for storing booked appointments
def init_booking_db():
    conn = sqlite3.connect("appointments.db")
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS appointment (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            patient_name TEXT,
            doctor_name TEXT,
            date TEXT,
            time TEXT,
            booking_timestamp TEXT
        )        
    """)
    conn.commit()
    conn.close()

init_booking_db()

# Load the Doctor database CSV
try:
    df = pd.read_csv("./doctors.csv")
    df.columns = df.columns.str.strip()
except Exception as e:
    print(f"Error loading doctors.csv: {e}. Make sure the file is in this folder!")  
    exit(1) 

# 2. DEFINING THE BOOKING TOOL FOR GEMINI

def book_appointment(patient_name: str, doctor_name: str, date: str, time: str) -> str:
    """
    Saves an appointment booking in the local SQL database.
    Args:
        patient_name: Name of the patient booking the slot
        doctor_name: Full name of the doctor
        date: Date of appointment (e.g., '2026-07-20')
        time: Requested time (e.g., '10:30 AM')
    """
    try:
        conn = sqlite3.connect("appointments.db")
        cursor = conn.cursor()
        # FIX: Changed 'appointments' to 'appointment' to match the schema creation
        cursor.execute(
            "INSERT INTO appointment (patient_name, doctor_name, date, time, booking_timestamp) VALUES (?, ?, ?, ?, ?)",
            (patient_name, doctor_name, date, time, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        )
        conn.commit()
        conn.close()
        success_msg = f"SUCCESS: Appointment booked for {patient_name} with {doctor_name} on {date} at {time}."
        print(f"\n[SYSTEM DATABASE TRIGGER]: {success_msg}\n")
        return success_msg
    except Exception as e:
        return f"FAILED: Could not book appointment due to database error: {e}"
    
# 3. Voice Engine 
"""
Imagine your program needs to:

Send text to Edge TTS.
Wait for the speech to be generated.
Play the audio.

Generating speech involves waiting for a response. While waiting, you don't want your whole program to freeze.
async def speak_async(text):
"""
async def speak_async(text):
    """Generates premium neural voice for free and plays it back."""
    print(f"Agent: {text}")
    op_file = "response.mp3"
    voice = "en-US-AriaNeural"

    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(op_file)
    pygame.mixer.music.load(op_file)
    pygame.mixer.music.play()
    while pygame.mixer.music.get_busy():
        await asyncio.sleep(0.1)
    pygame.mixer.music.unload()
    try:
        os.remove(op_file)
    except OSError:
        pass
def speak(text):
    """Helper to run the async voice function inside a synchronous loop."""
    asyncio.run(speak_async(text))

# 4. Speech - text engine
def listen_to_patient():
    """Listens via your microphone using standard free Google Speech Recognition API."""
    recognizer = sr.Recognizer()
    with sr.Microphone() as source:
        print("\n[Listening...] Ask a question or request a booking.")
        recognizer.adjust_for_ambient_noise(source, duration=0.8)
        try:
            audio = recognizer.listen(source, timeout=6, phrase_time_limit=8)
            print("[Processing audio...]")
            text = recognizer.recognize_google(audio)
            print(f"Patient said: {text}")
            return text
        except sr.WaitTimeoutError:
            return None
        except sr.UnknownValueError:
            speak("I didn't quite catch that. Could you please say it again?")
            return None
        except sr.RequestError:
            speak("I am having trouble accessing the network listener right now.")
            return None
        
# 5. CONTEXT GENERATOR & AI PROCESSING
def get_doctor_context():
    """Formats the CSV rows into raw text context for the model."""
    context = "Clinic Schedule Data:\n"
    for _, row in df.iterrows():
        context += (
            f"- {row['name']} ({row['specialization']}) at {row['hospital']}. "
            f"Experience: {row['experience']} years. Location: {row['location']}. "
            f"Days: {row['available_days']}, Hours: {row['available_time']}. "
            f"Fee: {row['consultation_fee']} INR. Phone: {row['phone']}.\n"
        )
    return context

# Initialize a global list to store all conversation context in memory
conversation_history = []

def ask_ai_brain(patient_query):
    """Sends user query to Gemini alongside a clean text history for rock-solid memory."""
    global conversation_history
    
    db_data = get_doctor_context()
    
    system_instruction = (
        "You are an elite, compassionate AI front-desk manager for City Hospital. "
        "Your duty is to assist patients by parsing out matching doctors from the data provided below, "
        "and booking appointments when they ask.\n\n"
        "Strict Booking Rules:\n"
        "1. You must collect 4 items: Patient Name, Doctor Name, Date, and Time.\n"
        "2. Review the history. Ask for ONLY ONE missing item at a time. Do not ask for everything at once.\n"
        "3. Remember information previously stated by the patient (e.g., if they already gave their name, do not ask for it again).\n"
        "4. Trigger the `book_appointment` tool ONLY when you have explicitly collected all 4 items.\n\n"
        "Strict Guidelines:\n"
        "1. Answer ONLY based on the facts provided in the data.\n"
        "2. Keep spoken responses short, warm, and punchy (1-2 sentences maximum).\n"
        "3. Never output symbols like asterisks (*), hashtags, bullet points, or markdown formatting.\n"
        f"\n[Hospital Database]\n{db_data}"
    )    
    
    # 1. Append the incoming user text safely to our history tracking list
    conversation_history.append(types.Content(role="user", parts=[types.Part.from_text(text=patient_query)]))
    
    try:
        response = client.models.generate_content(
            model='gemini-3.1-flash-lite', 
            contents=conversation_history,
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                temperature=0.2,
                tools=[book_appointment]
            )
        )
        
        # 2. Handle Tool Execution if all slots are finally full
        if response.function_calls:
            for call in response.function_calls:
                if call.name == "book_appointment":
                    args = call.args
                    tool_result = book_appointment(
                        patient_name=args.get("patient_name"),
                        doctor_name=args.get("doctor_name"),
                        date=args.get("date"),
                        time=args.get("time")
                    )
                    
                    # Track function call in history
                    conversation_history.append(response.candidates[0].content)
                    function_response_content = types.Content(
                        role="tool",
                        parts=[types.Part.from_function_response(name="book_appointment", response={"result": tool_result})]
                    )
                    conversation_history.append(function_response_content)
                    
                    follow_up = client.models.generate_content(
                        model='gemini-3.1-flash-lite',
                        contents=conversation_history,
                        config=types.GenerateContentConfig(system_instruction=system_instruction, temperature=0.2)
                    )
                    
                    # Clean extract text for the final follow up response
                    text_parts = [part.text for part in follow_up.candidates[0].content.parts if part.text]
                    clean_text = "".join(text_parts).strip()
                    clean_text = re.sub(r'[*#_-]', '', clean_text)
                    
                    # Append ONLY clean text back to the history history
                    conversation_history.append(types.Content(role="model", parts=[types.Part.from_text(text=clean_text)]))
                    return clean_text
                    
        # 3. Standard turn text extraction (safely filters out thought_signatures)
        text_parts = [part.text for part in response.candidates[0].content.parts if part.text]
        clean_text = "".join(text_parts).strip()
        clean_text = re.sub(r'[*#_-]', '', clean_text)
        
        # CRITICAL FIX: Append ONLY a clean text block to history. Remove the raw content object
        # which contained the hidden thinking signatures that were confusing the model.
        conversation_history.append(types.Content(role="model", parts=[types.Part.from_text(text=clean_text)]))
        
        return clean_text
        
    except Exception as e:
        print(f"Error handling Gemini API: {e}")
        return "I am unable to process that request right now."    

# 6. Execute 
if __name__ == "__main__":
    speak("Hello! Welcome to City Hospital Virtual Assistant. How can I help you today?")
    while True:
        patient_input = listen_to_patient()
        if patient_input:
            if any(term in patient_input.lower() for term in ["exit", "goodbye", "quit", "thank you bye"]):
                speak("Thank you for calling City Hospital. Wishing you great health. Goodbye!")
                break
                
            ai_reply = ask_ai_brain(patient_input)
            if ai_reply:
                speak(ai_reply)
                time.sleep(1.5)


"""
Agent: Hello! Welcome to City Hospital Virtual Assistant. How can I help you today?

[Listening...] Ask a question or request a booking.
[Processing audio...]
Patient said: I need to see dentist
Agent: I can certainly help you with that. May I have your name, please?

[Listening...] Ask a question or request a booking.
[Processing audio...]
Patient said: my name is Shweta
Agent: Nice to meet you, Shweta. What date would you like to see Dr. Meera Joshi?

[Listening...] Ask a question or request a booking.
[Processing audio...]
Agent: I didn't quite catch that. Could you please say it again?

[Listening...] Ask a question or request a booking.
[Processing audio...]
Patient said: on 17th 12:00 p.m.

[SYSTEM DATABASE TRIGGER]: SUCCESS: Appointment booked for Shweta with Dr. Meera Joshi on 2026-08-17 at 12:00 PM.

Agent: Your appointment with Dr. Meera Joshi is confirmed for August 17th at 12:00 PM. We look forward to seeing you then, Shweta.

[Listening...] Ask a question or request a booking.
[Processing audio...]
Patient said: thank you goodbye
Agent: Thank you for calling City Hospital. Wishing you great health. Goodbye!
"""                