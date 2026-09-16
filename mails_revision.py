import os
import time
import json
import base64
import threading
from datetime import datetime
from email.mime.text import MIMEText
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from google import genai
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

load_dotenv()
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
PERFIL = os.getenv('PERFIL', 'secretaria_academica')
INTERVALO_SEGUNDOS = int(os.getenv('INTERVALO_SEGUNDOS', '30'))
MAX_REINTENTOS = 3

with open('config_facultad.json') as f:
    CONFIG_FACULTAD = json.load(f)

CARPETA_NUEVOS = 'correos_nuevos'
CARPETA_REVISAR = 'para_revisar'
CARPETA_APROBADOS = 'aprobados'
CARPETA_ENVIADOS = 'enviados'

SCOPES = ['https://www.googleapis.com/auth/gmail.modify']
client_ai = genai.Client(api_key=GEMINI_API_KEY)

def registrar_log(mensaje):
    hora = datetime.now().strftime('%H:%M:%S')
    linea = f'[{hora}] {mensaje}'
    print(linea)
    with open('log_mails.txt', 'a') as f:
        f.write(linea + '\n')

def obtener_credenciales():
    creds = None
    if os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json', SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
            creds = flow.run_local_server(port=0)
        with open('token.json', 'w') as token:
            token.write(creds.to_json())
    return creds

def extraer_cuerpo(payload):
    if 'parts' in payload:
        for part in payload['parts']:
            if part.get('mimeType') == 'text/plain':
                data = part['body'].get('data')
                if data:
                    return base64.urlsafe_b64decode(data).decode('utf-8', errors='ignore')
        for part in payload['parts']:
            if 'parts' in part:
                resultado = extraer_cuerpo(part)
                if resultado:
                    return resultado
    else:
        data = payload['body'].get('data')
        if data:
            return base64.urlsafe_b64decode(data).decode('utf-8', errors='ignore')
    return ''

def ya_existe_en_pipeline(msg_id):
    for carpeta in (CARPETA_NUEVOS, CARPETA_REVISAR, CARPETA_APROBADOS, CARPETA_ENVIADOS):
        if os.path.exists(os.path.join(carpeta, f'{msg_id}.json')):
            return True
    return False

def clasificar_mail(asunto, cuerpo):
    info_confirmada = json.dumps(CONFIG_FACULTAD, ensure_ascii=False)
    prompt = (
        f"Sos un asistente de {PERFIL.replace('_', ' ')} de una facultad. "
        "Analiza esta consulta de un alumno y decidi si es 'trivial' "
        "(pregunta general: horarios, feriados, ubicaciones, fechas) "
        "o 'humano' (requiere decision administrativa puntual: casos, reclamos, excepciones). "
        f"Solo tenes esta informacion confirmada para responder: {info_confirmada}. "
        "Si la pregunta es trivial y la respuesta esta en esa informacion, generala usando SOLO esos datos. "
        "Si es trivial pero no tenes el dato confirmado, responde que no tenes esa informacion "
        "confirmada y que consulte directamente en secretaria. "
        "Si es humano, genera un mensaje breve avisando que fue derivada "
        "y que si no hay respuesta en 3 dias se acerque a secretaria. "
        f"Asunto: {asunto}\nCuerpo: {cuerpo}\n"
        'Responde SOLO en JSON: {"categoria": "trivial o humano", "respuesta": "texto"}'
    )
    for intento in range(1, MAX_REINTENTOS + 1):
        try:
            respuesta = client_ai.models.generate_content(
                model="gemini-3.5-flash-lite",
                contents=[prompt]
            )
            texto = respuesta.text.strip().replace('```json', '').replace('```', '').strip()
            datos = json.loads(texto)
            return datos.get('categoria', 'sin_clasificar'), datos.get('respuesta', '')
        except Exception as e:
            mensaje_error = str(e)
            es_temporal = '503' in mensaje_error or 'UNAVAILABLE' in mensaje_error or '429' in mensaje_error
            if es_temporal and intento < MAX_REINTENTOS:
                espera = 2 * intento
                registrar_log(f'⏳ IA ocupada, reintentando en {espera}s...')
                time.sleep(espera)
                continue
            registrar_log(f'⚠️ Error al clasificar: {e}')
            return 'sin_clasificar', ''

def derivar_a_administrativo(servicio, asunto, remitente, cuerpo_original):
    destino = CONFIG_FACULTAD.get('derivaciones', {}).get('default')
    if not destino:
        return
    mensaje = MIMEText(f"Caso derivado del sistema automatico.\n\nDe: {remitente}\nAsunto original: {asunto}\n\n{cuerpo_original}")
    mensaje['to'] = destino
    mensaje['subject'] = f'Caso derivado: {asunto}'
    raw = base64.urlsafe_b64encode(mensaje.as_bytes()).decode()
    servicio.users().messages().send(userId='me', body={'raw': raw}).execute()

def recolectar_correos(servicio):
    resultados = servicio.users().messages().list(userId='me', maxResults=20, labelIds=['INBOX'], q='is:unread').execute()
    mensajes = resultados.get('messages', [])
    for m in mensajes:
        msg_id = m['id']
        if ya_existe_en_pipeline(msg_id):
            continue
        detalle = servicio.users().messages().get(userId='me', id=msg_id, format='full').execute()
        headers = detalle['payload']['headers']
        asunto = next((h['value'] for h in headers if h['name'] == 'Subject'), '(sin asunto)')
        remitente = next((h['value'] for h in headers if h['name'] == 'From'), '')
        message_id_header = next((h['value'] for h in headers if h['name'] == 'Message-ID'), None)
        cuerpo = extraer_cuerpo(detalle['payload'])
        datos = {
            'msg_id': msg_id,
            'thread_id': detalle['threadId'],
            'message_id_header': message_id_header,
            'remitente': remitente,
            'asunto': asunto,
            'cuerpo_original': cuerpo,
        }
        with open(os.path.join(CARPETA_NUEVOS, f'{msg_id}.json'), 'w', encoding='utf-8') as f:
            json.dump(datos, f, ensure_ascii=False, indent=2)
        registrar_log(f"📥 Nuevo correo guardado: {asunto}")

def ciclo_recoleccion(creds):
    while True:
        registrar_log('🔍 Revisando bandeja de entrada...')
        try:
            servicio = build('gmail', 'v1', credentials=creds)
            recolectar_correos(servicio)
        except Exception as e:
            registrar_log(f'⚠️ Error al revisar bandeja: {e}')
        time.sleep(INTERVALO_SEGUNDOS)

def procesar_correo_nuevo(ruta, creds):
    with open(ruta, encoding='utf-8') as f:
        datos = json.load(f)

    categoria, respuesta = clasificar_mail(datos['asunto'], datos['cuerpo_original'])
    datos['categoria'] = categoria
    datos['respuesta_generada'] = respuesta

    nombre = os.path.basename(ruta)
    with open(os.path.join(CARPETA_REVISAR, nombre), 'w', encoding='utf-8') as f:
        json.dump(datos, f, ensure_ascii=False, indent=2)

    if categoria == 'humano':
        try:
            servicio = build('gmail', 'v1', credentials=creds)
            derivar_a_administrativo(servicio, datos['asunto'], datos['remitente'], datos['cuerpo_original'])
        except Exception as e:
            registrar_log(f'⚠️ No se pudo derivar el caso: {e}')

    os.remove(ruta)
    registrar_log(f"🧠 Clasificado '{datos['asunto']}' -> {categoria} -> para_revisar/ (esperando tu aprobación)")

class ManejadorNuevos(FileSystemEventHandler):
    def __init__(self, creds):
        self.creds = creds

    def on_created(self, event):
        if event.is_directory or not event.src_path.endswith('.json'):
            return
        time.sleep(0.3)
        try:
            procesar_correo_nuevo(event.src_path, self.creds)
        except Exception as e:
            registrar_log(f'⚠️ Error clasificando {event.src_path}: {e}')

def enviar_respuesta(servicio, datos, remitente, asunto, texto):
    subject = asunto if asunto.lower().startswith('re:') else f'Re: {asunto}'
    mensaje = MIMEText(texto)
    mensaje['to'] = remitente
    mensaje['subject'] = subject
    if datos.get('message_id_header'):
        mensaje['In-Reply-To'] = datos['message_id_header']
        mensaje['References'] = datos['message_id_header']
    raw = base64.urlsafe_b64encode(mensaje.as_bytes()).decode()
    cuerpo_envio = {'raw': raw, 'threadId': datos['thread_id']}
    servicio.users().messages().send(userId='me', body=cuerpo_envio).execute()

def procesar_aprobado(ruta, creds):
    with open(ruta, encoding='utf-8') as f:
        datos = json.load(f)
    servicio = build('gmail', 'v1', credentials=creds)
    enviar_respuesta(servicio, datos, datos['remitente'], datos['asunto'], datos['respuesta_generada'])
    servicio.users().messages().modify(userId='me', id=datos['msg_id'], body={'removeLabelIds': ['UNREAD']}).execute()
    nombre = os.path.basename(ruta)
    os.rename(ruta, os.path.join(CARPETA_ENVIADOS, nombre))
    registrar_log(f"📤 Aprobado y enviado: {datos['asunto']}")

class ManejadorAprobados(FileSystemEventHandler):
    def __init__(self, creds):
        self.creds = creds

    def on_created(self, event):
        if event.is_directory or not event.src_path.endswith('.json'):
            return
        time.sleep(0.3)
        try:
            procesar_aprobado(event.src_path, self.creds)
        except Exception as e:
            registrar_log(f'⚠️ Error enviando {event.src_path}: {e}')

def main():
    creds = obtener_credenciales()

    for carpeta in (CARPETA_NUEVOS, CARPETA_REVISAR, CARPETA_APROBADOS, CARPETA_ENVIADOS):
        os.makedirs(carpeta, exist_ok=True)

    threading.Thread(target=ciclo_recoleccion, args=(creds,), daemon=True).start()

    obs_nuevos = Observer()
    obs_nuevos.schedule(ManejadorNuevos(creds), CARPETA_NUEVOS, recursive=False)
    obs_nuevos.start()

    obs_aprobados = Observer()
    obs_aprobados.schedule(ManejadorAprobados(creds), CARPETA_APROBADOS, recursive=False)
    obs_aprobados.start()

    registrar_log(f'👀 Sistema iniciado. Perfil: {PERFIL}')
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        obs_nuevos.stop()
        obs_aprobados.stop()
    obs_nuevos.join()
    obs_aprobados.join()

if __name__ == '__main__':
    main()
