import os
import time
import json
import base64
import queue
import threading
from datetime import datetime
from email.mime.text import MIMEText
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from google import genai

load_dotenv()
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
PERFIL = os.getenv('PERFIL', 'secretaria_academica')
INTERVALO_SEGUNDOS = int(os.getenv('INTERVALO_SEGUNDOS', '30'))
MAX_REINTENTOS = 3

with open('config_facultad.json') as f:
    CONFIG_FACULTAD = json.load(f)

SCOPES = ['https://www.googleapis.com/auth/gmail.modify']
client_ai = genai.Client(api_key=GEMINI_API_KEY)
lock = threading.Lock()
procesados = set()

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

def enviar_respuesta(servicio, detalle, remitente, asunto, texto):
    subject = asunto if asunto.lower().startswith('re:') else f'Re: {asunto}'
    mensaje = MIMEText(texto)
    mensaje['to'] = remitente
    mensaje['subject'] = subject
    headers = detalle['payload']['headers']
    message_id_original = next((h['value'] for h in headers if h['name'] == 'Message-ID'), None)
    if message_id_original:
        mensaje['In-Reply-To'] = message_id_original
        mensaje['References'] = message_id_original
    raw = base64.urlsafe_b64encode(mensaje.as_bytes()).decode()
    cuerpo_envio = {'raw': raw, 'threadId': detalle['threadId']}
    servicio.users().messages().send(userId='me', body=cuerpo_envio).execute()

def marcar_como_leido(servicio, msg_id):
    servicio.users().messages().modify(userId='me', id=msg_id, body={'removeLabelIds': ['UNREAD']}).execute()

def derivar_a_administrativo(servicio, asunto, remitente, cuerpo_original):
    destino = CONFIG_FACULTAD.get('derivaciones', {}).get('default')
    if not destino:
        return
    mensaje = MIMEText(f"Caso derivado del sistema automatico.\n\nDe: {remitente}\nAsunto original: {asunto}\n\n{cuerpo_original}")
    mensaje['to'] = destino
    mensaje['subject'] = f'Caso derivado: {asunto}'
    raw = base64.urlsafe_b64encode(mensaje.as_bytes()).decode()
    servicio.users().messages().send(userId='me', body={'raw': raw}).execute()

def procesar_mensaje(servicio, msg_id):
    detalle = servicio.users().messages().get(userId='me', id=msg_id, format='full').execute()
    headers = detalle['payload']['headers']
    asunto = next((h['value'] for h in headers if h['name'] == 'Subject'), '(sin asunto)')
    remitente = next((h['value'] for h in headers if h['name'] == 'From'), '')
    cuerpo = extraer_cuerpo(detalle['payload'])

    categoria, respuesta_generada = clasificar_mail(asunto, cuerpo)
    carpeta = {'trivial': 'Trivial_respondido', 'humano': 'Requiere_humano'}.get(categoria, 'Sin_clasificar')

    with lock:
        os.makedirs(carpeta, exist_ok=True)

    with open(f'{carpeta}/{msg_id}.txt', 'w') as f:
        f.write(f"De: {remitente}\nAsunto: {asunto}\n\n{cuerpo}\n\n--- Respuesta generada ---\n{respuesta_generada}")

    if categoria == 'humano':
        try:
            derivar_a_administrativo(servicio, asunto, remitente, cuerpo)
        except Exception as e:
            registrar_log(f'⚠️ No se pudo derivar el caso: {e}')

    if categoria in ('trivial', 'humano') and respuesta_generada:
        try:
            enviar_respuesta(servicio, detalle, remitente, asunto, respuesta_generada)
            registrar_log(f"📩 '{asunto}' -> {categoria} -> {carpeta}/ -> respondido automáticamente")
        except Exception as e:
            registrar_log(f"⚠️ No se pudo enviar la respuesta a '{asunto}': {e}")
    else:
        registrar_log(f"📩 '{asunto}' -> {categoria} -> {carpeta}/")

    try:
        marcar_como_leido(servicio, msg_id)
    except Exception as e:
        registrar_log(f'⚠️ No se pudo marcar como leído {msg_id}: {e}')

def worker(cola, creds):
    servicio_propio = build('gmail', 'v1', credentials=creds)
    while True:
        msg_id = cola.get()
        if msg_id is None:
            break
        try:
            procesar_mensaje(servicio_propio, msg_id)
        except Exception as e:
            registrar_log(f'⚠️ Error procesando {msg_id}: {e}')
        cola.task_done()

def revisar_bandeja(servicio, cola):
    resultados = servicio.users().messages().list(userId='me', maxResults=20, labelIds=['INBOX'], q='is:unread').execute()
    mensajes = resultados.get('messages', [])
    for m in mensajes:
        with lock:
            if m['id'] not in procesados:
                procesados.add(m['id'])
                cola.put(m['id'])

def ciclo_scheduling(creds, cola):
    servicio_propio = build('gmail', 'v1', credentials=creds)
    while True:
        registrar_log('🔍 Revisando bandeja de entrada...')
        revisar_bandeja(servicio_propio, cola)
        time.sleep(INTERVALO_SEGUNDOS)

def main():
    creds = obtener_credenciales()
    cola = queue.Queue()

    for _ in range(3):
        threading.Thread(target=worker, args=(cola, creds), daemon=True).start()

    threading.Thread(target=ciclo_scheduling, args=(creds, cola), daemon=True).start()

    registrar_log(f'👀 Sistema iniciado con perfil: {PERFIL}')
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        registrar_log('🛑 Sistema detenido')

if __name__ == '__main__':
    main()
