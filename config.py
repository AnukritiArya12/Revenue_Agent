
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent

REPORTS_DIR = BASE_DIR / "data" / "reports"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

MODEL_NAME = os.getenv(
    "OLLAMA_MODEL", "qwen3.5:4b"
)

OLLAMA_BASE_URL = os.getenv(
    "OLLAMA_BASE_URL",
    "http://localhost:11434",
)

MODEL_TEMPERATURE = 0
MAX_FILE_SIZE_MB = 200
