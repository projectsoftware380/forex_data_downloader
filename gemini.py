import os, sys, google.generativeai as genai
from google.api_core.exceptions import NotFound

KEY = (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or "").strip()
if not KEY:
    raise SystemExit("Falta GEMINI_API_KEY/GOOGLE_API_KEY.")
genai.configure(api_key=KEY)

name = (os.getenv("GEMINI_MODEL") or "gemini-1.5-flash").strip()
# Normaliza nombres para clientes v1beta (no soportan '-latest')
if name.startswith("models/"):
    name = name.split("/", 1)[1]
if name.endswith("-latest"):
    name = name[:-7]
if name in {"flash", "1.5-flash"}:
    name = "gemini-1.5-flash"
if name in {"pro", "gemini-pro-latest"}:
    name = "gemini-pro"

prompt = " ".join(sys.argv[1:]) or "Di OK si me oyes."

def run(model_name):
    mdl = genai.GenerativeModel(model_name)
    return mdl.generate_content(prompt)

try:
    resp = run(name)
except NotFound:
    # Fallback: elige un modelo disponible con generateContent
    available = [
        m.name for m in genai.list_models()
        if "generateContent" in getattr(m, "supported_generation_methods", [])
    ]
    fallback = next((m for m in available if "gemini-1.5-flash" in m), None) \
            or next((m for m in available if "gemini-pro" in m), None)
    if not fallback:
        raise SystemExit(f"Modelo '{name}' no disponible. Modelos disponibles: {available}")
    resp = run(fallback)

print(getattr(resp, "text", resp))
