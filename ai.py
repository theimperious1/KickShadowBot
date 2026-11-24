import requests

OLLAMA_MODEL = "qwen3:8b"
OLLAMA_URL = "http://localhost:11434/api/chat"

def ask_ollama(prompt: str, system: str) -> str:
    response = requests.post(
        OLLAMA_URL,
        json={
            "model": OLLAMA_MODEL,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt}
            ],
            "keep_alive": 0,
            "stream": False,
            "options": {
                "temperature": 0.2,
                "num_ctx": 16384
            }
        },
        timeout=900
    )

    response.raise_for_status()

    return response.json()["message"]["content"].strip()