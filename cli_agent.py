#!/usr/bin/env python3
"""
cli_agent.py - Agente CLI local estilo "Claude Code" (minimalista)

Capacidades:
  - Leer archivos
  - Crear / escribir archivos
  - Modificar archivos (reemplazo de texto)
  - Ejecutar comandos de terminal (decididos por el modelo)
  - Modo manual: cualquier línea que empiece con "!" se ejecuta
    directamente en la shell, sin pasar por el modelo.

Usa el endpoint /api/chat de Ollama con tool calling nativo (parámetro
"tools"), no JSON simulado dentro del texto. El modelo debe soportar tools
(ej. qwen2.5-coder, qwen3-coder, llama3.1, etc. - ver `ollama show <modelo>`
y buscar "tools" en Capabilities). Requiere Ollama 0.3.11 o superior.
"""

import sys
import os
import json
import time
import shlex
import uuid
import queue
import signal
import atexit
import argparse
import threading
import subprocess
import requests

try:
    # Habilita edición de línea estilo bash (flechas, Home/End, historial
    # con flecha arriba/abajo, Ctrl+A/E, etc.) en Linux y macOS. En Windows
    # este módulo no existe, así que lo ignoramos si falla el import.
    import readline  # noqa: F401
except ImportError:
    pass

# ----------------------------------------------------------------------
# Configuración
# ----------------------------------------------------------------------
MODEL_NAME = os.environ.get("CLI_AGENT_MODEL", "qwen2.5-coder:7b-instruct-q4_K_M")
OLLAMA_URL = os.environ.get("CLI_AGENT_URL", "http://localhost:11434/api/chat")

# Modo autónomo: cuando está activo, las acciones consideradas "seguras"
# (recon / solo-lectura) se ejecutan sin pedir confirmación. Las de riesgo
# (destructivas, fuera de la lista blanca, sobrescritura de archivos) siguen
# pidiendo confirmación aunque el modo autónomo esté encendido. Se activa con
# el flag --auto, la env CLI_AGENT_AUTO=1, o el comando /auto en interactivo.
AUTO_MODE = os.environ.get("CLI_AGENT_AUTO", "").strip().lower() in ("1", "true", "yes", "on", "si", "sí")

# Directorio donde vive este script (no el directorio actual desde el que se
# invoque), para que "system_prompt.txt" siempre se busque junto al script.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# Ruta por defecto del archivo de system prompt: system_prompt.txt en el
# mismo directorio que cli_agent.py. Se puede sobreescribir con la variable
# de entorno CLI_AGENT_SYSTEM_FILE o con el flag --system-prompt.
DEFAULT_SYSTEM_PROMPT_FILE = os.environ.get(
    "CLI_AGENT_SYSTEM_FILE",
    os.path.join(SCRIPT_DIR, "system_prompt.md"),
)

GREEN = "\033[0;32m"
BLUE = "\033[0;34m"
YELLOW = "\033[0;33m"
RED = "\033[0;31m"
NC = "\033[0m"

# Prompt de respaldo, usado solo si no se encuentra ningún archivo de system prompt.
FALLBACK_SYSTEM_PROMPT = """Eres un asistente de desarrollo que corre en una terminal local.
Tienes acceso a herramientas (tools) para leer archivos, crear o modificar
archivos, y ejecutar comandos en la shell del usuario. Úsalas cuando la
instrucción requiera una acción física sobre el sistema; el usuario revisa y
confirma cada acción antes de que se ejecute, así que puedes proponerlas con
confianza.

Si la instrucción es solo una pregunta o no requiere ninguna acción física,
responde directamente con texto, de forma breve y clara.

Después de usar una herramienta verás su resultado. Decide entonces si hace
falta otro paso para completar la instrucción original (usa otra herramienta)
o si la tarea ya está completa (responde con texto resumiendo qué pasó, sin
usar más herramientas).
"""


def load_system_prompt(path: str) -> str:
    """Carga el system prompt desde un archivo de texto.

    Si el archivo no existe, avisa y usa el prompt de respaldo, de forma
    que el agente nunca se quede sin instrucciones base.
    """
    path = os.path.expanduser(path)
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read().strip()
        if not content:
            print(f"{YELLOW}[!] El archivo de system prompt está vacío: {path}. Usando el prompt por defecto.{NC}")
            return FALLBACK_SYSTEM_PROMPT
        print(f"{GREEN}[+] System prompt cargado desde: {path}{NC}")
        return content
    except FileNotFoundError:
        print(f"{YELLOW}[!] No se encontró '{path}'. Usando el system prompt por defecto.{NC}")
        return FALLBACK_SYSTEM_PROMPT
    except Exception as e:
        print(f"{RED}[!] Error al leer el system prompt ({e}). Usando el prompt por defecto.{NC}")
        return FALLBACK_SYSTEM_PROMPT


# Se reasigna en main() según el archivo indicado (flag, env var o default).
SYSTEM_PROMPT = FALLBACK_SYSTEM_PROMPT


# ----------------------------------------------------------------------
# Definición de las herramientas (formato estándar de function calling,
# el mismo que usan Ollama, OpenAI y Claude)
# ----------------------------------------------------------------------
TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Ejecuta un comando en la terminal del sistema del usuario.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "El comando de shell a ejecutar.",
                    },
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Crea un archivo nuevo o sobrescribe uno existente con el contenido dado.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Ruta del archivo a escribir."},
                    "content": {"type": "string", "description": "Contenido completo a escribir en el archivo."},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Modifica un archivo existente reemplazando un fragmento de texto por otro.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Ruta del archivo a modificar."},
                    "old": {"type": "string", "description": "Texto exacto a buscar y reemplazar."},
                    "new": {"type": "string", "description": "Texto nuevo que reemplaza al anterior."},
                },
                "required": ["path", "old", "new"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Lee y devuelve el contenido de un archivo existente.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Ruta del archivo a leer."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "task_complete",
            "description": (
                "Llama a esta herramienta cuando el objetivo del usuario ya se "
                "haya cumplido por completo. Detiene el bucle automático. "
                "Incluye un resumen de lo que se logró."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "description": "Resumen de lo que se logró."},
                },
                "required": ["summary"],
            },
        },
    },
]


# ----------------------------------------------------------------------
# Listado de modelos disponibles en Ollama (para el comando /model)
# ----------------------------------------------------------------------
def list_ollama_models() -> list:
    """Devuelve la lista (ordenada) de nombres de modelos instalados en
    Ollama, consultando el endpoint /api/tags. Ante cualquier error de
    conexión o parseo, avisa y devuelve una lista vacía."""
    tags_url = OLLAMA_URL.replace("/api/chat", "/api/tags")
    try:
        resp = requests.get(tags_url, timeout=5)
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.ConnectionError:
        print(f"{RED}[!] No se pudo conectar con Ollama en {tags_url}.{NC}")
        return []
    except (requests.exceptions.RequestException, ValueError) as e:
        print(f"{RED}[!] No se pudo obtener la lista de modelos: {e}{NC}")
        return []
    return sorted(m.get("name", "") for m in data.get("models", []) if m.get("name"))


# ----------------------------------------------------------------------
# Comunicación con el modelo (tool calling nativo vía /api/chat)
# ----------------------------------------------------------------------
def ask_model_chat(messages: list) -> dict:
    """Envía la conversación al modelo vía /api/chat con `tools`, en modo
    streaming. Imprime el texto token por token a medida que llega (si el
    modelo responde con texto) y, al terminar, la velocidad en tokens/s.

    Devuelve {"content": str, "tool_calls": list | None}. Si hay un error
    de conexión, devuelve None.
    """
    payload = {
        "model": MODEL_NAME,
        "messages": messages,
        "tools": TOOLS_SCHEMA,
        "stream": True,
    }

    full_content = ""
    buffer = ""
    streaming_started = False
    maybe_fallback_call = False
    tool_calls = None
    eval_count = None
    eval_duration = None
    start_time = time.monotonic()

    try:
        with requests.post(OLLAMA_URL, json=payload, stream=True, timeout=120) as resp:
            resp.raise_for_status()
            for raw_line in resp.iter_lines(decode_unicode=True):
                if not raw_line:
                    continue
                try:
                    chunk = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue

                message = chunk.get("message", {}) or {}
                piece = message.get("content", "")
                full_content += piece

                # Algunos modelos/plantillas no completan bien el tool_calls
                # nativo y devuelven la llamada como texto plano tipo
                # ```json {"name": ..., "arguments": {...}}```. Mientras no
                # sepamos cuál es el caso, retenemos los primeros caracteres
                # sin imprimir para poder detectarlo.
                if piece and not streaming_started and not maybe_fallback_call:
                    buffer += piece
                    stripped = buffer.lstrip()
                    if stripped.startswith(FALLBACK_JSON_MARKER):
                        maybe_fallback_call = True
                    elif len(stripped) >= len(FALLBACK_JSON_MARKER):
                        print(buffer, end="", flush=True)
                        streaming_started = True
                elif piece and streaming_started:
                    print(piece, end="", flush=True)
                # si maybe_fallback_call es True, no se imprime nada: se
                # decide al final si era realmente una llamada a herramienta

                if message.get("tool_calls"):
                    tool_calls = message["tool_calls"]

                if chunk.get("done"):
                    eval_count = chunk.get("eval_count")
                    eval_duration = chunk.get("eval_duration")
                    break
    except requests.exceptions.ConnectionError:
        print(f"{RED}[!] No se pudo conectar con Ollama en {OLLAMA_URL}.{NC}")
        return None
    except requests.exceptions.RequestException as e:
        print(f"{RED}[!] Error al consultar el modelo: {e}{NC}")
        return None

    # Respuesta de texto corta que nunca llegó al largo del marcador.
    if not streaming_started and not maybe_fallback_call and buffer:
        print(buffer, end="", flush=True)
        streaming_started = True

    # El modelo no usó tool_calls nativo, pero el contenido parece ser una
    # llamada a herramienta en texto plano: se intenta reinterpretar.
    if tool_calls is None and (maybe_fallback_call or full_content.lstrip().startswith(FALLBACK_JSON_MARKER)):
        fallback = _parse_fallback_tool_calls(full_content)
        if fallback:
            tool_calls = fallback
            full_content = ""
        elif maybe_fallback_call:
            # Parecía una llamada pero no se pudo interpretar: mostrarla tal
            # cual para no perder la respuesta del modelo.
            print(buffer, end="")
            streaming_started = True

    if streaming_started or full_content:
        print()

    elapsed = time.monotonic() - start_time
    tokens_per_sec = None
    if eval_count and eval_duration:
        tokens_per_sec = eval_count / (eval_duration / 1e9)
    elif eval_count and elapsed > 0:
        tokens_per_sec = eval_count / elapsed

    if tokens_per_sec is not None:
        print(f"{BLUE}[{tokens_per_sec:.1f} tok/s · {eval_count} tokens en {elapsed:.1f}s]{NC}")
    print()

    return {"content": full_content.strip(), "tool_calls": tool_calls}


# Marcador que usan algunos modelos cuando, por una plantilla de chat que no
# soporta bien tool calling nativo en Ollama, devuelven la llamada a función
# como texto plano en vez de en el campo estructurado "tool_calls".
FALLBACK_JSON_MARKER = "```json"


# Nombres de herramientas válidos, usados para reconocer la variante
# "{"action": "<tool>", ...campos...}" del formato de respaldo.
KNOWN_TOOL_NAMES = {tool["function"]["name"] for tool in TOOLS_SCHEMA}


def _parse_single_fallback_call(item: dict):
    """Interpreta un único objeto como una llamada a herramienta, aceptando
    dos formas distintas que distintos modelos/plantillas suelen producir
    cuando no completan bien el tool_calls nativo:

      1. {"name": "<tool>", "arguments": {...}}   (estilo OpenAI/Ollama)
      2. {"action": "<tool>", <campo1>: ..., ...}  (estilo genérico)

    Devuelve {"function": {"name": ..., "arguments": {...}}} o None.
    """
    if not isinstance(item, dict):
        return None

    name = item.get("name")
    arguments = item.get("arguments", item.get("parameters"))
    if name and isinstance(arguments, dict):
        return {"function": {"name": name, "arguments": arguments}}

    action = item.get("action")
    if action in KNOWN_TOOL_NAMES:
        arguments = {k: v for k, v in item.items() if k != "action"}
        return {"function": {"name": action, "arguments": arguments}}

    return None


def _parse_fallback_tool_calls(content: str):
    """Busca un bloque ```json {...}``` en el texto y lo interpreta como
    tool_calls (ver `_parse_single_fallback_call` para los formatos
    aceptados). Acepta también una lista de esos objetos. Devuelve una
    lista en el mismo formato que usa Ollama, o None si no aplica."""
    marker = "```json"
    if marker not in content:
        return None
    start = content.find(marker) + len(marker)
    end = content.find("```", start)
    if end == -1:
        return None
    raw = content[start:end].strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None

    candidates = parsed if isinstance(parsed, list) else [parsed]
    calls = [c for c in (_parse_single_fallback_call(item) for item in candidates) if c]
    return calls or None


# ----------------------------------------------------------------------
# Acciones (con confirmación del usuario)
# ----------------------------------------------------------------------
def confirm(message: str) -> bool:
    # Si no hay una terminal interactiva, no podemos preguntar: por seguridad
    # tratamos la ausencia de respuesta como un "no".
    if not sys.stdin.isatty():
        print(f"{YELLOW}{message} [s/N]: (sin TTY, se asume N){NC}")
        return False
    try:
        answer = input(f"{YELLOW}{message} [s/N]: {NC}").strip().lower()
    except EOFError:
        return False
    return answer in ("s", "si", "sí", "y", "yes")


# ----------------------------------------------------------------------
# Clasificación de seguridad (para el modo autónomo)
# ----------------------------------------------------------------------
# Binarios de solo-lectura / reconocimiento que pueden ejecutarse sin
# confirmación cuando el modo autónomo está activo.
RECON_ALLOWLIST = {
    "ls", "pwd", "cat", "head", "tail", "less", "more", "grep", "egrep",
    "fgrep", "find", "locate", "which", "whereis", "file", "stat", "wc",
    "sort", "uniq", "cut", "awk", "tr", "echo", "date", "whoami", "id",
    "uname", "hostname", "env", "printenv", "ps", "ss", "netstat", "ip",
    "ifconfig", "arp", "dig", "nslookup", "host", "whois", "ping",
    "traceroute", "tracepath", "nmap", "masscan", "rustscan", "gobuster",
    "feroxbuster", "ffuf", "dirb", "nikto", "whatweb", "wafw00f",
    "enum4linux", "smbmap", "rpcclient", "dnsenum", "dnsrecon", "fierce",
    "amass", "theHarvester", "curl", "wget", "searchsploit", "strings",
    "xxd", "base64", "md5sum", "sha1sum", "sha256sum", "jq",
}

# Binarios que SIEMPRE exigen confirmación, aunque alguien los agregue a la
# lista blanca: modifican el sistema, elevan privilegios o son destructivos.
DESTRUCTIVE = {
    "rm", "rmdir", "dd", "mkfs", "shred", "mv", "chmod", "chown", "kill",
    "pkill", "killall", "reboot", "shutdown", "systemctl", "service",
    "mount", "umount", "iptables", "nft", "useradd", "userdel", "passwd",
    "apt", "apt-get", "dpkg", "pip", "pip3", "npm", "tee", "truncate",
    "sudo",
}

# Operadores de shell que separan comandos encadenados.
_CHAIN_SPLIT = ("&&", "||", ";", "|")


def is_command_safe(command: str) -> bool:
    """Decide si un comando de shell puede ejecutarse sin confirmación en modo
    autónomo. Conservador: ante cualquier duda, devuelve False (pedir confirmación).
    """
    if not command or not command.strip():
        return False

    # Sustitución de comandos o redirecciones: pueden ocultar/derivar efectos
    # (ejecutar otro comando, sobrescribir archivos) -> siempre confirmar.
    if "`" in command or "$(" in command or ">" in command:
        return False

    # Partir en segmentos por los operadores de encadenamiento.
    segments = [command]
    for op in _CHAIN_SPLIT:
        segments = [s for seg in segments for s in seg.split(op)]

    for seg in segments:
        seg = seg.strip()
        if not seg:
            continue
        try:
            tokens = shlex.split(seg)
        except ValueError:
            return False  # comillas sin cerrar, etc. -> fail-safe
        if not tokens:
            return False
        # Saltar asignaciones de entorno al inicio (VAR=val cmd ...).
        idx = 0
        while idx < len(tokens) and "=" in tokens[idx] and not tokens[idx].startswith("-"):
            name = tokens[idx].split("=", 1)[0]
            if name and all(c.isalnum() or c == "_" for c in name):
                idx += 1
            else:
                break
        if idx >= len(tokens):
            return False
        base = os.path.basename(tokens[idx])
        if base in DESTRUCTIVE or base not in RECON_ALLOWLIST:
            return False

    return True


def is_write_safe(path: str) -> bool:
    """Escribir un archivo es 'seguro' (auto) solo si se trata de crear uno
    nuevo; sobrescribir uno existente exige confirmación."""
    return not os.path.exists(os.path.expanduser(path))


def auto_or_confirm(is_safe: bool, message: str) -> bool:
    """Puerta de confirmación. En modo autónomo, una acción segura pasa sin
    preguntar; todo lo demás cae en la confirmación manual habitual."""
    if AUTO_MODE and is_safe:
        print(f"{GREEN}[auto] {message} -> sí (acción segura){NC}")
        return True
    return confirm(message)


# Tope de caracteres del resultado de una acción que se retroalimenta al
# modelo, para no inflar el prompt con salidas enormes (ej. un build largo).
MAX_RESULT_CHARS = 4000


def _truncate(text: str) -> str:
    if len(text) > MAX_RESULT_CHARS:
        return text[:MAX_RESULT_CHARS] + f"\n[...salida truncada a {MAX_RESULT_CHARS} caracteres...]"
    return text


# ----------------------------------------------------------------------
# Bash interno persistente (estilo Claude Code)
# ----------------------------------------------------------------------
# Timeout máximo (segundos) que se espera a que un comando termine antes de
# matarlo, y cada cuánto se sondea si ya terminó (mostrando que sigue vivo).
def _env_float(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, default))
        return value if value > 0 else default
    except (ValueError, TypeError):
        return default


CMD_TIMEOUT = _env_float("CLI_AGENT_CMD_TIMEOUT", 300.0)
POLL_INTERVAL = _env_float("CLI_AGENT_POLL_INTERVAL", 2.0)


class PersistentShell:
    """Una sola sesión de bash viva durante toda la ejecución del agente, de
    forma que el estado (cwd, variables de entorno, etc.) persiste entre
    comandos. Cada comando se ejecuta hasta terminar; mientras tanto se sondea
    periódicamente y se muestra la salida en vivo. Si supera el timeout, se
    mata el proceso y se reinicia la shell."""

    def __init__(self):
        self.proc = None
        self.reader = None
        self.q = None
        self.token = None
        self._start()

    def _start(self):
        self.q = queue.Queue()
        self.token = f"__CLIAGENT_DONE_{uuid.uuid4().hex}__"
        self.proc = subprocess.Popen(
            ["bash"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,  # grupo de procesos propio (para killpg)
        )
        self.reader = threading.Thread(target=self._read_loop, daemon=True)
        self.reader.start()

    def _read_loop(self):
        """Lee stdout línea a línea y las encola, para que leer nunca bloquee
        al hilo principal."""
        try:
            for line in self.proc.stdout:
                self.q.put(line)
        except (ValueError, OSError):
            pass
        finally:
            self.q.put(None)  # señal de EOF (la shell murió)

    def _kill(self):
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        try:
            if self.proc and self.proc.stdout:
                self.proc.stdout.close()
        except OSError:
            pass

    def run(self, command: str, timeout: float = None, poll_interval: float = None):
        """Ejecuta `command` en la shell persistente. Devuelve
        (exit_code, salida_combinada). exit_code es None si hubo timeout o la
        shell murió. La salida se imprime en vivo mientras llega."""
        timeout = CMD_TIMEOUT if timeout is None else timeout
        poll_interval = POLL_INTERVAL if poll_interval is None else poll_interval

        # Si la shell murió por un timeout previo, reiniciarla.
        if self.proc is None or self.proc.poll() is not None:
            self._start()

        # Enviar el comando seguido del marcador que lleva el código de salida.
        try:
            self.proc.stdin.write(command + "\n")
            self.proc.stdin.write(f'printf "\\n%s %s\\n" "{self.token}" "$?"\n')
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError):
            self._start()
            return None, "La shell no estaba disponible; se reinició. Reintenta el comando."

        output = []
        start = time.monotonic()
        while True:
            try:
                line = self.q.get(timeout=poll_interval)
            except queue.Empty:
                elapsed = time.monotonic() - start
                if elapsed > timeout:
                    self._kill()
                    self._start()  # dejar lista una shell nueva
                    partial = "".join(output)
                    print(f"\n{RED}[!] Timeout tras {timeout:.0f}s: comando cortado.{NC}\n")
                    return None, partial + f"\n[comando cortado por timeout tras {timeout:.0f}s]"
                print(f"{BLUE}[... sigue ejecutando, {elapsed:.0f}s ...]{NC}")
                continue

            if line is None:
                # La shell murió inesperadamente.
                self._start()
                return None, "".join(output) + "\n[la shell terminó inesperadamente]"

            # ¿Es la línea del marcador de fin?
            if self.token in line:
                code = None
                parts = line.strip().split()
                if len(parts) >= 2 and parts[-1].lstrip("-").isdigit():
                    code = int(parts[-1])
                return code, "".join(output)

            print(line, end="", flush=True)
            output.append(line)

    def close(self):
        self._kill()


# Instancia global perezosa de la shell persistente.
_SHELL = None


def get_shell() -> PersistentShell:
    global _SHELL
    if _SHELL is None:
        _SHELL = PersistentShell()
        atexit.register(_SHELL.close)
    return _SHELL


def run_command(command: str):
    """Ejecuta un comando con confirmación en la shell persistente. Devuelve el
    resultado como texto (para retroalimentar al modelo), o None si el usuario
    canceló."""
    print(f"{YELLOW}[*] Comando propuesto:{NC} {command}")
    if not auto_or_confirm(is_command_safe(command), "¿Ejecutar este comando?"):
        print("Cancelado.\n")
        return None
    try:
        code, salida = get_shell().run(command)
        print()
        code_str = "None (timeout o shell caída)" if code is None else str(code)
        result = f"Código de salida: {code_str}\nSALIDA:\n{salida.strip()}"
        return _truncate(result)
    except Exception as e:
        print(f"{RED}[!] Error al ejecutar el comando: {e}{NC}\n")
        return f"Error al ejecutar el comando: {e}"


def write_file(path: str, content: str):
    path = os.path.expanduser(path)
    print(f"{YELLOW}[*] Se va a escribir el archivo:{NC} {path}")
    if not auto_or_confirm(is_write_safe(path), "¿Confirmar escritura?"):
        print("Cancelado.\n")
        return None
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as f:
            f.write(content)
        print(f"{GREEN}[+] Archivo escrito correctamente.{NC}\n")
        return f"Archivo '{path}' escrito correctamente ({len(content)} caracteres)."
    except Exception as e:
        print(f"{RED}[!] Error al escribir el archivo: {e}{NC}\n")
        return f"Error al escribir '{path}': {e}"


def edit_file(path: str, old: str, new: str):
    path = os.path.expanduser(path)
    print(f"{YELLOW}[*] Se va a modificar el archivo:{NC} {path}")
    # edit_file siempre muta contenido existente: nunca es "auto" seguro.
    if not auto_or_confirm(False, "¿Confirmar modificación?"):
        print("Cancelado.\n")
        return None
    try:
        with open(path, "r") as f:
            data = f.read()
        if old not in data:
            print(f"{RED}[!] El texto a reemplazar no se encontró en el archivo.{NC}\n")
            return f"No se encontró el texto a reemplazar en '{path}'."
        data = data.replace(old, new)
        with open(path, "w") as f:
            f.write(data)
        print(f"{GREEN}[+] Archivo modificado correctamente.{NC}\n")
        return f"Archivo '{path}' modificado correctamente."
    except FileNotFoundError:
        print(f"{RED}[!] El archivo no existe: {path}{NC}\n")
        return f"El archivo '{path}' no existe."
    except Exception as e:
        print(f"{RED}[!] Error al modificar el archivo: {e}{NC}\n")
        return f"Error al modificar '{path}': {e}"


def read_file(path: str):
    path = os.path.expanduser(path)
    try:
        with open(path, "r") as f:
            content = f.read()
        print(f"\n--- Contenido de {path} ---")
        print(content)
        print("---------------------------\n")
        return _truncate(f"Contenido de '{path}':\n{content}")
    except FileNotFoundError:
        print(f"{RED}[!] El archivo no existe: {path}{NC}\n")
        return f"El archivo '{path}' no existe."
    except Exception as e:
        print(f"{RED}[!] Error al leer el archivo: {e}{NC}\n")
        return f"Error al leer '{path}': {e}"


def call_tool(name: str, args: dict):
    """Ejecuta la herramienta pedida por el modelo y devuelve el resultado
    como texto (para retroalimentarlo), o None si no hay nada que
    retroalimentar (acción cancelada por el usuario o tool desconocida)."""
    if name == "run_command":
        return run_command(args.get("command", ""))
    elif name == "write_file":
        return write_file(args.get("path", ""), args.get("content", ""))
    elif name == "edit_file":
        return edit_file(args.get("path", ""), args.get("old", ""), args.get("new", ""))
    elif name == "read_file":
        return read_file(args.get("path", ""))
    else:
        print(f"{RED}[!] Herramienta desconocida: {name}{NC}\n")
        return None


# ----------------------------------------------------------------------
# Bucle principal
# ----------------------------------------------------------------------
# Máximo de pasos automáticos encadenados (tool call -> resultado -> siguiente
# tool call) antes de detenerse, para evitar que el modelo quede en bucle.
# Configurable con la env CLI_AGENT_MAX_STEPS o el flag --max-steps.
def _default_max_steps() -> int:
    try:
        value = int(os.environ.get("CLI_AGENT_MAX_STEPS", "25"))
        return value if value > 0 else 25
    except ValueError:
        return 25


MAX_AGENT_STEPS = _default_max_steps()


def _parse_tool_args(raw_args):
    """Los argumentos de un tool_call pueden venir como dict o como string
    JSON, según el modelo. Normaliza siempre a dict."""
    if isinstance(raw_args, dict):
        return raw_args
    if isinstance(raw_args, str):
        try:
            return json.loads(raw_args)
        except json.JSONDecodeError:
            return {}
    return {}


def handle_prompt(user_prompt: str):
    """Procesa una instrucción como una conversación de chat con tool
    calling nativo, encadenando tool call -> resultado -> siguiente tool
    call mientras el modelo lo pida, hasta MAX_AGENT_STEPS o hasta que
    responda con texto normal (sin más llamadas a herramientas)."""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    for step in range(1, MAX_AGENT_STEPS + 1):
        reply = ask_model_chat(messages)
        if reply is None:
            return

        tool_calls = reply.get("tool_calls")
        if not tool_calls:
            # Sin herramientas y sin texto: el modelo no devolvió nada. Suele
            # ser incompatibilidad del modelo con el system prompt (plantillas
            # GGUF que rompen con el rol 'system'). Avisamos en vez de dejar
            # un blanco silencioso.
            if not reply.get("content", "").strip():
                print(f"{YELLOW}[!] El modelo '{MODEL_NAME}' no devolvió ninguna "
                      f"respuesta. Puede ser incompatible con el system prompt "
                      f"(plantilla o soporte de 'system' defectuoso). Prueba otro "
                      f"modelo con /model, o revisa su plantilla con "
                      f"'ollama show {MODEL_NAME}'.{NC}\n")
            # Respuesta de texto normal (ya impresa en streaming): no hay
            # más herramientas que encadenar, se termina aquí.
            return

        # Se registra el turno del asistente con sus tool_calls antes de
        # ejecutar nada, tal como lo espera el historial de /api/chat.
        messages.append({
            "role": "assistant",
            "content": reply.get("content", ""),
            "tool_calls": tool_calls,
        })

        any_executed = False
        for tc in tool_calls:
            fn = tc.get("function", {})
            name = fn.get("name", "")
            args = _parse_tool_args(fn.get("arguments", {}))

            # Señal de fin: el modelo declara el objetivo cumplido.
            if name == "task_complete":
                summary = args.get("summary", "").strip() or "(sin resumen)"
                print(f"{GREEN}[✓] Objetivo cumplido:{NC} {summary}\n")
                return

            result = call_tool(name, args)
            if result is None:
                # Cancelado por el usuario, o herramienta desconocida: se
                # informa al modelo y no se sigue encadenando después de
                # este turno.
                result = "Acción cancelada por el usuario o no ejecutada."
            else:
                any_executed = True

            messages.append({
                "role": "tool",
                "content": result,
                "tool_name": name,
            })

        if not any_executed:
            # Si el usuario canceló todas las acciones propuestas en este
            # turno, no tiene sentido seguir pidiendo más pasos.
            return

    print(f"{YELLOW}[!] Se alcanzó el límite de {MAX_AGENT_STEPS} pasos automáticos. "
          f"Puedes seguir pidiendo el próximo paso manualmente.{NC}\n")


def pick_model_at_startup():
    """Selector de modelo al arrancar el modo interactivo. Muestra los modelos
    disponibles con el actual marcado; Enter mantiene el actual. Devuelve el
    nombre elegido (o el actual si se mantiene / no hay con qué elegir)."""
    models = list_ollama_models()
    if not models:
        print(f"{YELLOW}[!] No hay modelos disponibles en Ollama. Se usará "
              f"'{MODEL_NAME}' (instala uno con 'ollama pull <modelo>').{NC}")
        return MODEL_NAME

    print(f"{BLUE}Elige el modelo a usar:{NC}")
    for i, name in enumerate(models, 1):
        marca = f" {GREEN}(actual){NC}" if name == MODEL_NAME else ""
        print(f"  {i}) {name}{marca}")
    try:
        choice = input(f"{YELLOW}Modelo (número o nombre; Enter = mantener '{MODEL_NAME}'): {NC}").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return MODEL_NAME

    if not choice:
        return MODEL_NAME
    if choice.isdigit():
        idx = int(choice)
        if 1 <= idx <= len(models):
            return models[idx - 1]
        print(f"{RED}[!] Número fuera de rango. Se mantiene '{MODEL_NAME}'.{NC}")
        return MODEL_NAME
    if choice in models:
        return choice
    print(f"{RED}[!] '{choice}' no está disponible. Se mantiene '{MODEL_NAME}'.{NC}")
    return MODEL_NAME


def switch_model():
    """Lógica del comando interactivo /model (sin argumento): muestra los
    modelos disponibles numerados y permite elegir por número o nombre.
    Devuelve el nombre elegido, o None si se cancela o no hay cambio."""
    models = list_ollama_models()
    if not models:
        print(f"{YELLOW}[!] No hay modelos disponibles. Instala uno con "
              f"'ollama pull <modelo>'.{NC}\n")
        return None

    print(f"{BLUE}Modelos disponibles:{NC}")
    for i, name in enumerate(models, 1):
        marca = f" {GREEN}*{NC}" if name == MODEL_NAME else ""
        print(f"  {i}) {name}{marca}")
    try:
        choice = input(f"{YELLOW}Elige modelo (número o nombre, vacío/q para cancelar): {NC}").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None

    if not choice or choice.lower() == "q":
        print("Cancelado.\n")
        return None

    if choice.isdigit():
        idx = int(choice)
        if 1 <= idx <= len(models):
            return models[idx - 1]
        print(f"{RED}[!] Número fuera de rango.{NC}\n")
        return None

    if choice in models:
        return choice

    print(f"{RED}[!] '{choice}' no está entre los modelos disponibles.{NC}\n")
    return None


def main():
    global SYSTEM_PROMPT, AUTO_MODE, MAX_AGENT_STEPS, MODEL_NAME

    parser = argparse.ArgumentParser(
        description="Agente CLI local con soporte de archivos, comandos y system prompt configurable."
    )
    parser.add_argument(
        "-s", "--system-prompt",
        dest="system_prompt_file",
        default=DEFAULT_SYSTEM_PROMPT_FILE,
        help=f"Ruta a un archivo de texto con el system prompt (default: {DEFAULT_SYSTEM_PROMPT_FILE})",
    )
    parser.add_argument(
        "--auto",
        action="store_true",
        help="Modo autónomo: ejecuta acciones de recon/solo-lectura sin confirmar; "
             "las destructivas o fuera de la lista blanca siguen pidiendo confirmación.",
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        default=None,
        help=f"Máximo de pasos automáticos encadenados (default: {MAX_AGENT_STEPS}).",
    )
    parser.add_argument(
        "prompt",
        nargs="*",
        help="Instrucción a ejecutar en modo de un solo disparo. Si se omite, entra en modo interactivo.",
    )
    args = parser.parse_args()

    if args.auto:
        AUTO_MODE = True
    if args.max_steps is not None and args.max_steps > 0:
        MAX_AGENT_STEPS = args.max_steps

    SYSTEM_PROMPT = load_system_prompt(args.system_prompt_file)

    if AUTO_MODE:
        print(f"{YELLOW}[!] Modo AUTÓNOMO activo: las acciones de recon/solo-lectura se "
              f"ejecutarán sin confirmación (las destructivas sí la pedirán).{NC}")

    # Uso: python3 cli_agent.py "instrucción"  -> modo de un solo disparo
    if args.prompt:
        handle_prompt(" ".join(args.prompt))
        return

    # Selector de modelo al inicio (solo en modo interactivo).
    MODEL_NAME = pick_model_at_startup()

    print(f"{BLUE}[*] Agente CLI local iniciado.{NC} "
          f"(modelo: {MODEL_NAME} · AUTO: {'on' if AUTO_MODE else 'off'})")
    print("Escribe una instrucción en lenguaje natural, o usa '!' seguido de un")
    print("comando para ejecutarlo directamente (modo manual). '/auto' activa/")
    print("desactiva el modo autónomo, '/model' cambia de modelo. 'exit' para salir.\n")

    while True:
        try:
            line = input(f"{GREEN}agent ❯ {NC}")
        except (KeyboardInterrupt, EOFError):
            print("\nSaliendo...")
            break

        stripped = line.strip()
        if not stripped:
            continue
        if stripped.lower() in ("exit", "quit"):
            break

        if stripped.lower() == "/auto":
            AUTO_MODE = not AUTO_MODE
            estado = "activado" if AUTO_MODE else "desactivado"
            print(f"{YELLOW}[*] Modo autónomo {estado}.{NC}\n")
            continue

        if stripped.lower().startswith("/model"):
            parts = stripped.split(maxsplit=1)
            if len(parts) == 2:
                # /model <nombre>: cambio directo si el modelo existe.
                nombre = parts[1].strip()
                disponibles = list_ollama_models()
                if not disponibles:
                    print(f"{YELLOW}[!] No hay modelos disponibles. Instala uno con "
                          f"'ollama pull <modelo>'.{NC}\n")
                elif nombre in disponibles:
                    MODEL_NAME = nombre
                    print(f"{GREEN}[+] Modelo activo: {MODEL_NAME}{NC}\n")
                else:
                    print(f"{RED}[!] '{nombre}' no está disponible. Modelos: "
                          f"{', '.join(disponibles)}{NC}\n")
            else:
                # /model: elegir de una lista interactiva.
                elegido = switch_model()
                if elegido:
                    MODEL_NAME = elegido
                    print(f"{GREEN}[+] Modelo activo: {MODEL_NAME}{NC}\n")
            continue

        if stripped.startswith("!"):
            # Modo manual: ejecuta directamente, sin pasar por el modelo
            command = stripped[1:].strip()
            if not command:
                continue
            run_command(command)
        else:
            handle_prompt(stripped)


if __name__ == "__main__":
    main()
