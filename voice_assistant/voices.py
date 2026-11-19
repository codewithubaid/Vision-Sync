import pyttsx3

engine = pyttsx3.init()

voices = engine.getProperty("voices")

print("Available voices:\n")

for i, voice in enumerate(voices):
    print(f"Voice {i}")
    print("Name:", voice.name)
    print("ID:", voice.id)
    print("Languages:", voice.languages)
    print("-" * 50)