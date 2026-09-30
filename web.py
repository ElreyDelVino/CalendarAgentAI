import os
import datetime
from zoneinfo import ZoneInfo
import streamlit as st
from dotenv import load_dotenv
from google import genai
from google.genai import types
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

load_dotenv()

# Permiso mínimo: solo eventos (no configuración del calendario ni otros calendarios)
SCOPES = ['https://www.googleapis.com/auth/calendar.events']
MODELO = 'gemini-3.5-flash-lite'
MAX_MENSAJES = 40  # límite por sesión para proteger tu cuota de Gemini
DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]


def config(nombre: str) -> str:
    """Lee un valor de st.secrets (en la nube) o de variables de entorno / .env (en local)."""
    try:
        return st.secrets[nombre]
    except Exception:
        return os.getenv(nombre, "")


APP_URL = config("APP_URL") or "http://localhost:8501"
if APP_URL.startswith("http://localhost"):
    os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"  # solo para pruebas locales


@st.cache_resource
def cliente_ia():
    return genai.Client(api_key=config("GEMINI_API_KEY"))


# ---------- Login con Google ----------
def crear_flow() -> Flow:
    cfg = {"web": {
        "client_id": config("GOOGLE_CLIENT_ID"),
        "client_secret": config("GOOGLE_CLIENT_SECRET"),
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": [APP_URL],
    }}
    # Sin PKCE: al volver de Google la sesión de Streamlit es nueva y no conservaría el verifier
    return Flow.from_client_config(cfg, scopes=SCOPES, redirect_uri=APP_URL,
                                   autogenerate_code_verifier=False)


def servicio():
    return build('calendar', 'v3', credentials=st.session_state.creds, cache_discovery=False)


def pedir_login():
    """Muestra la pantalla de login y detiene la app hasta que el usuario entre."""
    if "code" in st.query_params:  # Google nos devuelve aquí tras el login
        codigo = st.query_params["code"]
        st.query_params.clear()
        try:
            flow = crear_flow()
            flow.fetch_token(code=codigo)
            st.session_state.creds = flow.credentials
            st.session_state.zona = servicio().calendars().get(calendarId='primary').execute().get('timeZone', 'UTC')
        except Exception as e:
            st.session_state.pop("creds", None)
            st.error(f"No se pudo iniciar sesión: {e}")
        else:
            st.rerun()

    st.title("📅 Asistente de calendario")
    st.write("Gestiona tu Google Calendar hablando: crea, consulta, mueve o borra eventos con lenguaje natural.")
    url, _ = crear_flow().authorization_url(access_type="offline", prompt="consent")
    st.link_button("🔐 Iniciar sesión con Google", url, type="primary")
    st.caption("Solo se solicita acceso a tus eventos. No se guardan tus credenciales en el servidor.")
    st.stop()


# ---------- Herramientas para la IA (usan el calendario del usuario actual) ----------
def listar_eventos(dias: int = 14) -> str:
    """Lista los eventos de los próximos N días con su ID exacto. Úsala siempre antes de modificar o eliminar."""
    try:
        ahora = datetime.datetime.now(datetime.timezone.utc)
        res = servicio().events().list(
            calendarId='primary', timeMin=ahora.isoformat(),
            timeMax=(ahora + datetime.timedelta(days=dias)).isoformat(),
            maxResults=25, singleEvents=True, orderBy='startTime').execute()
        eventos = res.get('items', [])
        if not eventos:
            return "No hay eventos programados en ese periodo."
        return "\n".join(
            f"- ID: {e['id']} | {e['start'].get('dateTime', e['start'].get('date'))} | {e.get('summary', '(sin título)')}"
            for e in eventos)
    except Exception as e:
        return f"Error al listar: {e}"


def crear_evento(titulo: str, fecha_hora_inicio: str, fecha_hora_fin: str, recurrencia: str = "") -> str:
    """
    Crea un evento en el calendario del usuario.

    Args:
        titulo: El título del evento.
        fecha_hora_inicio: Inicio en formato ISO (ejemplo: 2026-09-25T10:00:00).
        fecha_hora_fin: Fin en formato ISO (ejemplo: 2026-09-25T11:00:00).
        recurrencia: Opcional. Regla RRULE, por ejemplo "RRULE:FREQ=WEEKLY;INTERVAL=2;BYDAY=MO".
    """
    try:
        zona = st.session_state.zona
        evento = {'summary': titulo,
                  'start': {'dateTime': fecha_hora_inicio, 'timeZone': zona},
                  'end': {'dateTime': fecha_hora_fin, 'timeZone': zona}}
        if recurrencia:
            evento['recurrence'] = [recurrencia]
        servicio().events().insert(calendarId='primary', body=evento).execute()
        return f"Éxito. Evento '{titulo}' creado."
    except Exception as e:
        return f"Error al crear el evento: {e}"


def eliminar_evento(evento_id: str) -> str:
    """Elimina un evento usando su ID exacto."""
    try:
        servicio().events().delete(calendarId='primary', eventId=evento_id).execute()
        return "Éxito. Evento eliminado."
    except Exception as e:
        return f"Error al eliminar: {e}"


def modificar_evento(evento_id: str, nuevo_titulo: str, fecha_hora_inicio: str, fecha_hora_fin: str) -> str:
    """Modifica un evento existente usando su ID. Requiere título y fechas en formato ISO."""
    try:
        zona = st.session_state.zona
        cambios = {'summary': nuevo_titulo,
                   'start': {'dateTime': fecha_hora_inicio, 'timeZone': zona},
                   'end': {'dateTime': fecha_hora_fin, 'timeZone': zona}}
        servicio().events().patch(calendarId='primary', eventId=evento_id, body=cambios).execute()
        return "Éxito. Evento actualizado."
    except Exception as e:
        return f"Error al modificar: {e}"


def crear_chat():
    zona = st.session_state.zona
    h = datetime.datetime.now(ZoneInfo(zona))
    fecha = f"{DIAS[h.weekday()]} {h.strftime('%Y-%m-%d, %H:%M')}"
    instrucciones = f"""
    Eres un asistente virtual experto en la gestión de calendarios. Tono profesional, amigable y directo.
    Responde en el idioma del usuario y de forma breve.

    REGLA TEMPORAL: Ahora es {fecha} (zona {zona}). Usa solo esto para calcular "mañana", "el viernes", etc.

    Herramientas: 'listar_eventos', 'crear_evento', 'modificar_evento', 'eliminar_evento'.
    REGLA DE EDICIÓN: para modificar o eliminar, llama primero a 'listar_eventos', localiza el ID y pásalo.
    Nunca inventes IDs. Si la petición es ambigua (hora, día...), pregunta antes de actuar.
    REGLA DE RECURRENCIA: para eventos repetidos usa el parámetro 'recurrencia' en formato RRULE.
    """
    return cliente_ia().chats.create(model=MODELO, config=types.GenerateContentConfig(
        system_instruction=instrucciones,
        tools=[listar_eventos, crear_evento, eliminar_evento, modificar_evento],
        temperature=0.3))


def mostrar_privacidad():
    """Página pública de política de privacidad, en el mismo dominio que el resto de la app."""
    st.title("📅 Política de privacidad — Agente Calendario")
    st.caption("Última actualización: 29 de septiembre de 2026")
    st.markdown("""
Agente Calendario es una aplicación personal que permite gestionar tu Google Calendar mediante
lenguaje natural (crear, consultar, modificar y eliminar eventos) a través de un asistente de IA.

### Qué datos se usan
- Los eventos de tu Google Calendar (título, fecha, hora y, si aplica, reglas de recurrencia) que tú
  mismo pidas consultar, crear, modificar o eliminar durante la conversación.
- El texto que escribes al asistente, para poder interpretarlo y responder.

### Cómo se usan
Los datos de tu calendario se usan **únicamente** para responder a tus propias peticiones dentro de
la misma conversación. No se usan con ningún otro fin.

- El texto de tu mensaje y, cuando es necesario para responder, la información de tus eventos, se
  envían a la API de Gemini (Google) para generar la respuesta del asistente.
- Las acciones sobre tu calendario se realizan directamente contra la API de Google Calendar, con tu
  autorización explícita mediante el inicio de sesión de Google (OAuth).

### Qué NO se hace con tus datos
- No se almacenan tus credenciales de Google en el servidor: el acceso se mantiene solo durante tu
  sesión en el navegador.
- No se guarda un historial permanente de tus eventos ni de tus conversaciones en ninguna base de datos.
- No se comparten, venden ni ceden tus datos a terceros. No se usan para publicidad.

### Permisos solicitados
La aplicación solicita el permiso `https://www.googleapis.com/auth/calendar.events`, que permite
únicamente ver, crear, modificar y eliminar eventos de tu calendario. No se solicita acceso a la
configuración general del calendario ni a otros datos de tu cuenta de Google.

### Cómo revocar el acceso
Puedes retirar el acceso de esta aplicación a tu cuenta de Google en cualquier momento desde
[myaccount.google.com/permissions](https://myaccount.google.com/permissions).

### Contacto
Para cualquier duda sobre esta política, escribe a la dirección de correo de soporte indicada en
la propia aplicación.
    """)
    st.stop()


# ---------- Interfaz ----------
st.set_page_config(page_title="Asistente de calendario", page_icon="📅")

if st.query_params.get("page") == "privacidad":
    mostrar_privacidad()

if "creds" not in st.session_state:
    pedir_login()

st.title("📅 Asistente de calendario")

if "chat" not in st.session_state:
    st.session_state.chat = crear_chat()
    st.session_state.mensajes = [{"role": "assistant",
                                  "content": "¡Hola! Puedo ver, crear, modificar y eliminar eventos de tu calendario. ¿Qué necesitas?"}]
    st.session_state.contador = 0

with st.sidebar:
    if st.button("🗑️ Nueva conversación"):
        for k in ("chat", "mensajes", "contador"):
            st.session_state.pop(k, None)
        st.rerun()
    if st.button("🚪 Cerrar sesión"):
        st.session_state.clear()
        st.rerun()
    st.caption("Ejemplos:\n\n- ¿Qué tengo esta semana?\n- Crea una reunión mañana a las 10\n- Cambia el dentista al viernes")

for m in st.session_state.mensajes:
    with st.chat_message(m["role"]):
        st.markdown(m["content"])

if prompt := st.chat_input("Escribe tu mensaje..."):
    st.session_state.mensajes.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        if st.session_state.contador >= MAX_MENSAJES:
            texto = "Has alcanzado el límite de mensajes de esta sesión. Pulsa «Nueva conversación» para continuar."
        else:
            st.session_state.contador += 1
            with st.spinner("Pensando..."):
                try:
                    texto = st.session_state.chat.send_message(prompt).text
                except Exception as e:
                    texto = f"⚠️ Error con la IA: {e}"
        st.markdown(texto)
    st.session_state.mensajes.append({"role": "assistant", "content": texto})
