# offensive

Agente CLI local, estilo "Claude Code", que usa un modelo de [Ollama](https://ollama.com) corriendo en tu propia máquina. Permite leer, crear y editar archivos, y ejecutar comandos de terminal, con confirmación del usuario antes de cada acción (salvo en modo autónomo, donde solo las acciones de solo-lectura se saltan la confirmación).

## Archivos

- `cli_agent.py` — el agente. Habla con Ollama vía `/api/chat` usando tool calling nativo.
- `install.sh` — instalador: instala Ollama si falta, descarga el modelo, copia los archivos a `~/cli_agent` y registra un alias en `.bashrc`/`.zshrc`.
- `system_prompt.md` — system prompt por defecto del agente (editable).

## Instalación

```bash
chmod +x install.sh
./install.sh
source ~/.bashrc   # o ~/.zshrc
```

Variables opcionales antes de ejecutar `install.sh`:

| Variable       | Default                                   | Descripción                        |
|----------------|--------------------------------------------|-------------------------------------|
| `AGENT_MODEL`  | `huihui_ai/dolphin3-abliterated:latest`    | Modelo de Ollama a descargar/usar   |
| `AGENT_ALIAS`  | `agent`                                    | Alias del comando                   |
| `AGENT_DIR`    | `$HOME/cli_agent`                          | Carpeta de instalación              |

## Uso

```bash
agent
```

Al iniciar, el agente te deja elegir el modelo instalado en Ollama. Dentro de la sesión interactiva:

- Escribe cualquier instrucción en lenguaje natural; el modelo decide si responde con texto o propone una acción (leer/crear/editar archivo, ejecutar comando).
- Cada acción se muestra antes de ejecutarse y pide confirmación, salvo que estés en modo autónomo y la acción sea de solo lectura.
- Cualquier línea que empiece con `!` se ejecuta directamente en la shell, sin pasar por el modelo.
- `/auto` activa o desactiva el modo autónomo.
- `/model` cambia de modelo sin reiniciar.

## Configuración vía variables de entorno

| Variable                 | Descripción                                               |
|---------------------------|------------------------------------------------------------|
| `CLI_AGENT_MODEL`         | Modelo de Ollama a usar                                    |
| `CLI_AGENT_URL`           | Endpoint de Ollama (default `http://localhost:11434/api/chat`) |
| `CLI_AGENT_AUTO`          | `1`/`true` para arrancar en modo autónomo                  |
| `CLI_AGENT_SYSTEM_FILE`   | Ruta a un `system_prompt.md` alternativo                   |
| `CLI_AGENT_MAX_STEPS`     | Máximo de pasos del bucle agente (default `25`)            |

## Requisitos

- Python 3 + `requests`
- [Ollama](https://ollama.com) 0.3.11+ con un modelo que soporte tool calling (p. ej. `qwen2.5-coder`, `qwen3-coder`, `llama3.1`)

## Contexto de uso

`system_prompt.md` está orientado a tareas de pentesting/seguridad ofensiva en entornos de pruebas autorizados. Ajusta ese archivo si necesitas un propósito distinto.
