"""
openwebui_client.py — petit client pour l'API OpenAI-compatible d'Open WebUI
(POST {base_url}/api/chat/completions), utilisé par translate_dataset.py et
generate_new_entries.py.

Configuration via variables d'environnement (ou arguments CLI équivalents dans
les scripts appelants) :
    OPENWEBUI_BASE_URL   ex: http://localhost:3000  (sans /api/chat/completions à la fin)
    OPENWEBUI_API_KEY    généré dans Open WebUI: Paramètres -> Compte -> Clés API
"""
import json
import time
import urllib.error
import urllib.request


class OpenWebUIError(Exception):
    pass


def chat(base_url: str, api_key: str, model: str, system: str, user: str,
         max_retries: int = 3, timeout: int = 120) -> str:
    """Appelle /api/chat/completions et renvoie le texte de la réponse (content)."""
    url = base_url.rstrip("/") + "/api/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
    }
    body = json.dumps(payload).encode("utf-8")

    last_err = None
    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(
                url,
                data=body,
                method="POST",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return data["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            last_err = OpenWebUIError(f"HTTP {e.code}: {detail[:300]}")
        except Exception as e:
            last_err = e
        if attempt < max_retries - 1:
            time.sleep(1.5 * (attempt + 1))
    raise last_err