#!/bin/bash
#
# install.sh - Instalador para el agente CLI local (cli_agent.py)
#
# Instala Ollama (si falta), descarga un modelo, instala dependencias de
# Python, copia el agente a una carpeta fija y registra un alias.
#
# Variables configurables (puedes exportarlas antes de ejecutar el script,
# o simplemente editar los valores por defecto de abajo):
#   AGENT_MODEL   -> modelo de Ollama a usar (default: huihui_ai/dolphin3-abliterated:latest)
#   AGENT_ALIAS   -> nombre del comando rápido (default: agent)
#   AGENT_DIR     -> carpeta de instalación (default: $HOME/cli_agent)

set -e

GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[0;33m'
RED='\033[0;31m'
NC='\033[0m'

AGENT_MODEL="${AGENT_MODEL:-huihui_ai/dolphin3-abliterated:latest}"
AGENT_ALIAS="${AGENT_ALIAS:-agent}"
AGENT_DIR="${AGENT_DIR:-$HOME/cli_agent}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo -e "${BLUE}[*] Instalando agente CLI local${NC}"
echo -e "    Modelo:  ${AGENT_MODEL}"
echo -e "    Alias:   ${AGENT_ALIAS}"
echo -e "    Carpeta: ${AGENT_DIR}\n"

# ----------------------------------------------------------------------
# 1. Verificar que existan los archivos del agente junto a este script
# ----------------------------------------------------------------------
if [[ ! -f "$SCRIPT_DIR/cli_agent.py" ]]; then
    echo -e "${RED}[!] No se encontró cli_agent.py junto a install.sh. Abortando.${NC}"
    exit 1
fi

# ----------------------------------------------------------------------
# 2. Instalar Python3 y pip si hacen falta
# ----------------------------------------------------------------------
if ! command -v python3 &> /dev/null; then
    echo -e "${BLUE}[*] Instalando Python3...${NC}"
    sudo apt-get update -y
    sudo apt-get install -y python3 python3-pip
else
    echo -e "${GREEN}[+] Python3 ya está instalado.${NC}"
fi

# ----------------------------------------------------------------------
# 3. Instalar la librería requests
# ----------------------------------------------------------------------
# En Kali/Debian recientes pip está bloqueado por PEP 668
# (externally-managed-environment), así que preferimos el paquete de apt.
if python3 -c "import requests" &> /dev/null; then
    echo -e "${GREEN}[+] requests ya está instalado.${NC}"
else
    echo -e "${BLUE}[*] Instalando dependencias de Python (requests)...${NC}"
    if command -v apt-get &> /dev/null; then
        sudo apt-get install -y python3-requests
    else
        python3 -m pip install --user --quiet requests \
            || python3 -m pip install --user --quiet --break-system-packages requests
    fi
fi

# ----------------------------------------------------------------------
# 4. Instalar Ollama si no está presente
# ----------------------------------------------------------------------
if ! command -v ollama &> /dev/null; then
    echo -e "${BLUE}[*] Instalando Ollama...${NC}"
    curl -fsSL https://ollama.com/install.sh | sh
else
    echo -e "${GREEN}[+] Ollama ya está instalado.${NC}"
fi

# Asegurar que el servicio de Ollama esté activo
if command -v systemctl &> /dev/null && systemctl list-unit-files | grep -q ollama; then
    sudo systemctl start ollama 2>/dev/null || true
else
    # Si no hay systemd/servicio, lo levantamos en segundo plano
    if ! pgrep -x "ollama" > /dev/null; then
        echo -e "${BLUE}[*] Levantando 'ollama serve' en segundo plano...${NC}"
        nohup ollama serve > /tmp/ollama.log 2>&1 &
        sleep 3
    fi
fi

# ----------------------------------------------------------------------
# 5. Descargar el modelo
# ----------------------------------------------------------------------
echo -e "${BLUE}[*] Descargando el modelo '${AGENT_MODEL}' (puede tardar varios minutos)...${NC}"
ollama pull "$AGENT_MODEL"

# ----------------------------------------------------------------------
# 6. Copiar los archivos del agente a una carpeta fija
# ----------------------------------------------------------------------
echo -e "${BLUE}[*] Copiando archivos a ${AGENT_DIR}...${NC}"
mkdir -p "$AGENT_DIR"
cp "$SCRIPT_DIR/cli_agent.py" "$AGENT_DIR/cli_agent.py"

if [[ -f "$SCRIPT_DIR/system_prompt.md" ]]; then
    cp "$SCRIPT_DIR/system_prompt.md" "$AGENT_DIR/system_prompt.md"
else
    echo -e "${YELLOW}[!] No se encontró system_prompt.md junto a install.sh; el agente usará su prompt por defecto hasta que crees uno en ${AGENT_DIR}/system_prompt.md${NC}"
fi

# ----------------------------------------------------------------------
# 7. Registrar alias y variables de entorno en .bashrc / .zshrc
# ----------------------------------------------------------------------
register_alias() {
    local rc_file="$1"
    [[ -f "$rc_file" ]] || return 0

    # Eliminar el bloque gestionado previo (entre marcadores) para no duplicar.
    sed -i "/^# >>> cli_agent >>>$/,/^# <<< cli_agent <<<$/d" "$rc_file" 2>/dev/null || true

    # Limpieza de instalaciones antiguas (formato sin marcadores).
    sed -i "/^# --- cli_agent (instalado automáticamente) ---$/d" "$rc_file" 2>/dev/null || true
    sed -i "/^alias ${AGENT_ALIAS}=/d" "$rc_file" 2>/dev/null || true
    sed -i "/^export CLI_AGENT_MODEL=/d" "$rc_file" 2>/dev/null || true
    sed -i "/^export CLI_AGENT_SYSTEM_FILE=/d" "$rc_file" 2>/dev/null || true

    # Colapsar líneas en blanco múltiples al final del archivo.
    sed -i -e :a -e '/^\n*$/{$d;N;ba}' "$rc_file" 2>/dev/null || true

    {
        echo ""
        echo "# >>> cli_agent >>>"
        echo "export CLI_AGENT_MODEL=\"${AGENT_MODEL}\""
        echo "export CLI_AGENT_SYSTEM_FILE=\"${AGENT_DIR}/system_prompt.md\""
        echo "alias ${AGENT_ALIAS}='python3 ${AGENT_DIR}/cli_agent.py'"
        echo "# <<< cli_agent <<<"
    } >> "$rc_file"

    echo -e "${GREEN}[+] Alias registrado en ${rc_file}${NC}"
}

register_alias "$HOME/.bashrc"
register_alias "$HOME/.zshrc"

# ----------------------------------------------------------------------
# 8. Resumen final
# ----------------------------------------------------------------------
echo -e "\n${GREEN}================================================================${NC}"
echo -e "${GREEN}[+] Instalación completa${NC}"
echo -e "${GREEN}================================================================${NC}"
echo -e "Archivos instalados en: ${AGENT_DIR}"
echo -e "Para activar el alias en esta terminal ahora mismo, ejecuta:"
echo -e "${BLUE}  source ~/.bashrc${NC}   (o ~/.zshrc si usas zsh)"
echo -e "Luego, inicia el agente con:"
echo -e "${BLUE}  ${AGENT_ALIAS}${NC}\n"
echo -e "Puedes editar el system prompt en cualquier momento en:"
echo -e "${BLUE}  ${AGENT_DIR}/system_prompt.md${NC}\n"
