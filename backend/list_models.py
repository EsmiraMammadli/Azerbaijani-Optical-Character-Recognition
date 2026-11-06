import os
from dotenv import load_dotenv
from google import genai

load_dotenv()
c = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

for m in c.models.list():
    print(m.name)