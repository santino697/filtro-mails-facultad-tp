# Filtro inteligente de mails para una facultad

Sistema en Python que atiende automáticamente la casilla de mail administrativa de una facultad (simulada con una cuenta de Gmail de prueba). Lee los mails nuevos, los clasifica con IA (Google Gemini) y decide qué hacer con cada uno:

- **Consultas triviales** (feriados, horarios, fechas de inscripción): genera una respuesta usando **solo** datos confirmados de un archivo de configuración, sin inventar información.
- **Consultas que requieren una persona** (reclamos, excepciones, casos puntuales): las deriva por mail a un administrativo y le avisa al alumno que su caso fue derivado.

Proyecto desarrollado para la materia **Sistemas Operativos** (Ingeniería en Informática, USAL).

## Funcionamiento

```
Bandeja de Gmail ──► revisión periódica ──► cola de mensajes ──► hilos trabajadores
                                                                      │
                                                          clasificación con Gemini
                                                                      │
                                     ┌────────────────────────────────┴───────────────┐
                                  trivial                                           humano
                         responde con datos confirmados                  deriva al administrativo
                                                                         y avisa al alumno
```

## Dos modos de uso

| Script | Modo | Descripción |
|---|---|---|
| `clasificador_mails.py` | Automático | Clasifica y responde sin intervención. Usa una cola y 3 hilos trabajadores para procesar varios mails en paralelo. |
| `mails_revision.py` | Con aprobación humana | La IA propone la respuesta y la deja en `para_revisar/`. Al mover el archivo a `aprobados/`, el sistema lo detecta (watchdog) y envía el mail. |
| `prueba_conexion.py` | Prueba | Verifica la conexión con la API de Gmail listando los últimos mensajes. |

## Conceptos de Sistemas Operativos aplicados

- **Procesos e hilos:** hilos trabajadores concurrentes, cada uno con su propia conexión a la API (evita errores de conexión compartida).
- **Sincronización:** `threading.Lock` y cola thread-safe (`queue.Queue`) para no procesar el mismo mail dos veces.
- **Scheduling:** revisión periódica de la bandeja cada N segundos (configurable).
- **Automatización por eventos del sistema de archivos:** `watchdog` detecta archivos nuevos en carpetas y dispara acciones.
- **Manejo del sistema de archivos:** organización de mails en carpetas según su estado (`Trivial_respondido/`, `Requiere_humano/`, `Sin_clasificar/`).
- **Logs:** registro de cada acción en `log_mails.txt`.

## Manejo de errores

- Reintentos automáticos cuando la IA responde con error temporal (503 / 429), con espera creciente.
- Los mails procesados se marcan como leídos para no reprocesarlos (evita que el sistema se responda a sí mismo).
- Si la IA falla, el mail queda en `Sin_clasificar/` en lugar de perderse.

## Tecnologías

Python 3 · Gmail API (OAuth 2.0) · Google Gemini API · watchdog · python-dotenv · Ubuntu (máquina virtual)

## Instalación

1. Clonar el repositorio y crear un entorno virtual:
   ```bash
   git clone https://github.com/santino697/filtro-mails-facultad-tp.git
   cd filtro-mails-facultad-tp
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```
2. En Google Cloud, habilitar la **Gmail API**, crear credenciales OAuth de tipo "App de escritorio" y guardar el archivo como `credentials.json` en la carpeta del proyecto.
3. Copiar `.env.example` a `.env` y completar la API key de Gemini:
   ```bash
   cp .env.example .env
   ```
4. Editar `config_facultad.json` con la información confirmada de la facultad y el mail de derivación.
5. Ejecutar:
   ```bash
   python3 clasificador_mails.py     # modo automático
   python3 mails_revision.py         # modo con aprobación humana
   ```
   La primera vez se abre el navegador para autorizar el acceso a Gmail.

## Configuración

| Variable (`.env`) | Descripción | Valor por defecto |
|---|---|---|
| `GEMINI_API_KEY` | API key de Google Gemini | — |
| `PERFIL` | Perfil que atiende la casilla (cambia el comportamiento de la IA), ej. `secretaria_academica`, `bedelia` | `secretaria_academica` |
| `INTERVALO_SEGUNDOS` | Cada cuántos segundos se revisa la bandeja | `30` |

## Autor

Santino Tomás Rojas — Estudiante de Ingeniería en Informática (USAL)
